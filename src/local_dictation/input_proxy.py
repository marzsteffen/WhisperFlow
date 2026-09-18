"""Exclusive evdev input proxy used by local-dictation.

The helper owns the privileged/raw-input part of the application.  While it is
enabled it grabs every physical keyboard which can emit ``KEY_RIGHTCTRL``,
mirrors all other events to one uinput device per event node, and reports the
aggregate trigger state to its parent process.

This module deliberately does not import :mod:`evdev` or :mod:`pyudev` at
module import time.  They are loaded only when :class:`LinuxEvdevBackend` is
constructed in the helper process.  The state machine is consequently usable
with small fakes in unit tests and on development machines without access to
``/dev/input``.
"""

from __future__ import annotations

import errno
import importlib
import multiprocessing
import select
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# Linux input-event ABI values.  Keeping these constants here avoids importing
# python-evdev in the UI/parent process.
EV_SYN = 0
EV_KEY = 1
SYN_REPORT = 0
SYN_DROPPED = 3
KEY_RIGHTCTRL = 97

PROXY_NAME_PREFIX = "local-dictation proxy"


class InputBackendUnavailable(RuntimeError):
    """Raised when the Linux input dependencies cannot be loaded."""


@dataclass(frozen=True, slots=True)
class InputProxyConfig:
    """Runtime settings for the input helper.

    ``heartbeat_timeout_seconds`` may be set to zero in an isolated test or a
    diagnostic utility to disable the watchdog.  Production callers should
    keep the default and send heartbeats more frequently than the timeout.
    """

    trigger_code: int = KEY_RIGHTCTRL
    heartbeat_timeout_seconds: float = 2.5
    poll_interval_seconds: float = 0.05
    rescan_interval_seconds: float = 1.0
    grab_retry_seconds: float = 0.5
    grab_error_report_interval_seconds: float = 5.0
    proxy_name_prefix: str = PROXY_NAME_PREFIX

    @classmethod
    def from_value(
        cls, value: InputProxyConfig | Mapping[str, Any] | None
    ) -> InputProxyConfig:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("input proxy config must be a mapping")
        known = {field_.name for field_ in cls.__dataclass_fields__.values()}
        unknown = set(value) - known
        if unknown:
            names = ", ".join(sorted(str(item) for item in unknown))
            raise ValueError(f"unknown input proxy setting(s): {names}")
        return cls(**dict(value))


@dataclass(slots=True)
class PollBatch:
    """Events returned by an input backend for a single poll cycle."""

    events: dict[str, Sequence[Any]] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    rescan: bool = False


class InputBackend(Protocol):
    """The small backend surface consumed by :class:`InputProxyService`."""

    def discover_paths(self) -> set[str]: ...

    def open_device(self, path: str) -> Any: ...

    def create_proxy(self, source: Any, name: str) -> Any: ...

    def active_keys(self, source: Any) -> set[int]: ...

    def drain_events(self, source: Any) -> None: ...

    def grab(self, source: Any) -> None: ...

    def ungrab(self, source: Any) -> None: ...

    def forward_event(self, sink: Any, event: Any) -> None: ...

    def release_key(self, sink: Any, code: int) -> None: ...

    def sync(self, sink: Any) -> None: ...

    def close_device(self, device: Any) -> None: ...

    def poll(self, devices: Mapping[str, Any], timeout: float) -> PollBatch: ...

    def close(self) -> None: ...


def _device_node(udev_device: Any) -> str | None:
    node = getattr(udev_device, "device_node", None)
    if node:
        return str(node)
    properties = getattr(udev_device, "properties", {})
    try:
        node = properties.get("DEVNAME")
    except AttributeError:
        node = None
    return str(node) if node else None


def _sys_path(udev_device: Any) -> str:
    return str(
        getattr(udev_device, "sys_path", "")
        or getattr(udev_device, "device_path", "")
    )


def _is_virtual_input(udev_device: Any) -> bool:
    path = _sys_path(udev_device).replace("\\", "/")
    return "/devices/virtual/input/" in path


def _is_owned_name(name: str | None, prefix: str) -> bool:
    return bool(name) and str(name).casefold().startswith(prefix.casefold())


def _capability_codes(values: Any) -> set[int]:
    """Normalize evdev capability lists (which can contain ``(code, info)``)."""

    result: set[int] = set()
    for value in values or ():
        raw = value[0] if isinstance(value, tuple) else value
        try:
            result.add(int(raw))
        except (TypeError, ValueError):
            continue
    return result


class LinuxEvdevBackend:
    """Production backend backed by python-evdev and pyudev.

    Module injection is supported for tests.  Passing neither module performs
    lazy imports here, in the helper process rather than in the GUI process.
    """

    def __init__(
        self,
        *,
        trigger_code: int = KEY_RIGHTCTRL,
        proxy_name_prefix: str = PROXY_NAME_PREFIX,
        evdev_module: Any | None = None,
        pyudev_module: Any | None = None,
        select_fn: Callable[..., Any] = select.select,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            self._evdev = evdev_module or importlib.import_module("evdev")
            self._pyudev = pyudev_module or importlib.import_module("pyudev")
        except ImportError as exc:
            raise InputBackendUnavailable(
                "python-evdev und python-pyudev sind nicht installiert"
            ) from exc

        self.trigger_code = trigger_code
        self.proxy_name_prefix = proxy_name_prefix
        self._select = select_fn
        self._sleep = sleep_fn
        self._context = self._pyudev.Context()
        self._monitor: Any | None = None

        # The periodic reconciliation performed by InputProxyService remains a
        # fallback if netlink monitoring is unavailable.
        try:
            monitor = self._pyudev.Monitor.from_netlink(self._context)
            monitor.filter_by(subsystem="input")
            if hasattr(monitor, "start"):
                monitor.start()
            elif hasattr(monitor, "enable_receiving"):
                monitor.enable_receiving()
            self._monitor = monitor
        except (AttributeError, OSError):
            self._monitor = None

    def discover_paths(self) -> set[str]:
        paths: set[str] = set()
        for udev_device in self._context.list_devices(subsystem="input"):
            path = _device_node(udev_device)
            if (
                path is None
                or not Path(path).name.startswith("event")
                or _is_virtual_input(udev_device)
            ):
                continue

            source = None
            try:
                source = self._evdev.InputDevice(path)
                if _is_owned_name(getattr(source, "name", None), self.proxy_name_prefix):
                    continue
                capabilities = source.capabilities(verbose=False, absinfo=False)
                keys = _capability_codes(capabilities.get(EV_KEY, ()))
                if self.trigger_code in keys:
                    paths.add(path)
            except (FileNotFoundError, PermissionError, OSError):
                # A device can disappear between the udev enumeration and open.
                continue
            finally:
                if source is not None:
                    try:
                        source.close()
                    except OSError:
                        pass
        return paths

    def open_device(self, path: str) -> Any:
        return self._evdev.InputDevice(path)

    def create_proxy(self, source: Any, name: str) -> Any:
        return self._evdev.UInput.from_device(
            source,
            name=name,
            phys="local-dictation/input-proxy",
        )

    def active_keys(self, source: Any) -> set[int]:
        return {int(code) for code in source.active_keys(verbose=False)}

    def drain_events(self, source: Any) -> None:
        # evdev fds are non-blocking.  Cap the loop so a very busy keyboard
        # cannot starve control/heartbeat handling while waiting for a safe grab.
        for _ in range(32):
            try:
                events = list(source.read())
            except BlockingIOError:
                return
            if not events:
                return

    def grab(self, source: Any) -> None:
        source.grab()

    def ungrab(self, source: Any) -> None:
        source.ungrab()

    def forward_event(self, sink: Any, event: Any) -> None:
        sink.write_event(event)

    def release_key(self, sink: Any, code: int) -> None:
        sink.write(EV_KEY, code, 0)

    def sync(self, sink: Any) -> None:
        sink.syn()

    def close_device(self, device: Any) -> None:
        device.close()

    def _drain_monitor(self) -> tuple[bool, set[str]]:
        if self._monitor is None:
            return False, set()
        rescan = False
        removed: set[str] = set()
        while True:
            try:
                udev_device = self._monitor.poll(timeout=0)
            except OSError:
                self._monitor = None
                break
            if udev_device is None:
                break
            action = str(getattr(udev_device, "action", "") or "")
            path = _device_node(udev_device)
            if action == "remove" and path:
                removed.add(path)
            if action in {"add", "bind", "change", "move", "remove"}:
                rescan = True
        return rescan, removed

    def poll(self, devices: Mapping[str, Any], timeout: float) -> PollBatch:
        readers: dict[int, tuple[str, str | None]] = {}
        if self._monitor is not None:
            try:
                readers[int(self._monitor.fileno())] = ("monitor", None)
            except (OSError, TypeError, ValueError):
                self._monitor = None
        for path, source in devices.items():
            try:
                fd = int(source.fd)
            except (AttributeError, OSError, TypeError, ValueError):
                continue
            if fd >= 0:
                readers[fd] = ("device", path)

        if not readers:
            self._sleep(max(0.0, timeout))
            return PollBatch()

        try:
            readable, _, _ = self._select(list(readers), [], [], max(0.0, timeout))
        except (OSError, ValueError) as exc:
            if isinstance(exc, OSError) and exc.errno not in {errno.EBADF, errno.ENODEV}:
                raise
            removed = {
                path
                for path, source in devices.items()
                if getattr(source, "fd", -1) is None or getattr(source, "fd", -1) < 0
            }
            return PollBatch(removed=removed, rescan=True)

        batch = PollBatch()
        for fd in readable:
            kind, path = readers[int(fd)]
            if kind == "monitor":
                rescan, removed = self._drain_monitor()
                batch.rescan = batch.rescan or rescan
                batch.removed.update(removed)
                continue
            assert path is not None
            source = devices.get(path)
            if source is None:
                continue
            try:
                batch.events[path] = list(source.read())
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ENODEV, errno.EBADF, errno.EIO}:
                    batch.removed.add(path)
                    batch.rescan = True
                else:
                    raise
        return batch

    def close(self) -> None:
        self._monitor = None


@dataclass(slots=True)
class _ManagedDevice:
    path: str
    source: Any
    sink: Any
    grabbed: bool = False
    virtual_keys_down: set[int] = field(default_factory=set)
    next_grab_attempt: float = 0.0
    last_grab_error: int | None = None
    last_grab_error_at: float = float("-inf")


class InputProxyService:
    """Pure control/event state machine for the input helper process."""

    def __init__(
        self,
        backend: InputBackend,
        *,
        control: Any | None = None,
        events: Any | Callable[[dict[str, Any]], None] | None = None,
        config: InputProxyConfig | Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = InputProxyConfig.from_value(config)
        self.backend = backend
        self.control = control
        self.events = events
        self.clock = clock

        self.enabled = False
        self.stopping = False
        self.devices: dict[str, _ManagedDevice] = {}
        self.discovered_paths: set[str] = set()
        self.trigger_devices: set[str] = set()
        self.last_heartbeat = self.clock()
        self.next_scan = 0.0
        self._closed = False
        self._last_grab_state: tuple[bool, int, int] | None = None

    def _emit(self, message_type: str, **fields: Any) -> None:
        message = {"type": message_type, "timestamp": self.clock(), **fields}
        if self.events is None:
            return
        try:
            if callable(self.events):
                self.events(message)
            else:
                self.events.send(message)
        except (BrokenPipeError, EOFError, OSError):
            # The recipient is the supervising parent.  If it has gone away,
            # stop promptly so the finally block releases every EVIOCGRAB.
            self.stopping = True

    def rescan(self) -> None:
        try:
            discovered = self.backend.discover_paths()
        except OSError as exc:
            self._emit("error", code="input-scan-failed", message=str(exc), fatal=False)
            return

        # Keep failed opens/failed uinput creations in the readiness denominator.
        # Otherwise one working keyboard could mask a second discovered keyboard
        # that is not actually proxied.
        self.discovered_paths = set(discovered)
        for path in sorted(set(self.devices) - discovered):
            self._drop_device(path, reason="not-present")
        for path in sorted(discovered - set(self.devices)):
            self._adopt_device(path)

    def _adopt_device(self, path: str) -> None:
        source = None
        sink = None
        try:
            source = self.backend.open_device(path)
            proxy_name = f"{self.config.proxy_name_prefix} {Path(path).name}"
            sink = self.backend.create_proxy(source, proxy_name)
            self.devices[path] = _ManagedDevice(path=path, source=source, sink=sink)
            self._emit("device", action="added", path=path)
        except OSError as exc:
            if sink is not None:
                self._close_quietly(sink)
            if source is not None:
                self._close_quietly(source)
            self._emit(
                "error",
                code="input-device-open-failed",
                message=str(exc),
                path=path,
                fatal=False,
            )

    def set_enabled(self, enabled: bool, *, reason: str = "parent") -> None:
        if enabled:
            self.last_heartbeat = self.clock()
            if not self.enabled:
                self.enabled = True
                self.next_scan = 0.0
                self._emit("status", state="enabled", reason=reason)
            self._reconcile_grabs()
            return

        was_enabled = self.enabled
        self.enabled = False
        self._clear_trigger_state(reason=reason)
        for managed in list(self.devices.values()):
            self._ungrab_device(managed)
        if was_enabled:
            self._emit("status", state="disabled", reason=reason)
        self._emit_grab_state()

    def _safe_to_grab(self, managed: _ManagedDevice) -> bool:
        try:
            # Drop events accumulated while the real device was ungrabbed.  They
            # have already reached the compositor and must not be duplicated.
            self.backend.drain_events(managed.source)
            if self.backend.active_keys(managed.source):
                return False
            self.backend.grab(managed.source)
            # Close the check/grab race: if a key went down at the boundary,
            # immediately return control to the physical device and wait for an
            # all-keys-up cycle before trying again.
            if self.backend.active_keys(managed.source):
                self.backend.ungrab(managed.source)
                return False
            managed.grabbed = True
            managed.next_grab_attempt = 0.0
            managed.last_grab_error = None
            self._emit("device", action="grabbed", path=managed.path)
            return True
        except OSError as exc:
            if exc.errno in {errno.ENODEV, errno.EBADF, errno.EIO}:
                self._drop_device(managed.path, reason="device-removed")
            else:
                now = self.clock()
                managed.next_grab_attempt = now + self.config.grab_retry_seconds
                should_report = (
                    managed.last_grab_error != exc.errno
                    or now - managed.last_grab_error_at
                    >= self.config.grab_error_report_interval_seconds
                )
                managed.last_grab_error = exc.errno
                if should_report:
                    managed.last_grab_error_at = now
                    detail = {
                        errno.EBUSY: "device is already exclusively grabbed",
                        errno.EACCES: "permission denied",
                        errno.EPERM: "operation not permitted",
                    }.get(exc.errno, str(exc))
                    self._emit(
                        "error",
                        code="input-grab-failed",
                        message=detail,
                        path=managed.path,
                        errno=exc.errno,
                        retrying=True,
                        fatal=False,
                    )
            return False

    def _emit_grab_state(self) -> None:
        discovered = len(self.discovered_paths)
        grabbed = sum(item.grabbed for item in self.devices.values())
        snapshot = (self.enabled, discovered, grabbed)
        if snapshot == self._last_grab_state:
            return
        self._last_grab_state = snapshot
        if not self.enabled:
            state = "input-disabled"
        elif discovered == 0:
            state = "input-unavailable"
        elif grabbed == discovered:
            state = "input-ready"
        else:
            state = "input-partial"
        self._emit(
            "status",
            state=state,
            discovered=discovered,
            grabbed=grabbed,
            all_grabbed=bool(self.enabled and discovered and grabbed == discovered),
        )

    def _reconcile_grabs(self) -> None:
        if self.enabled:
            now = self.clock()
            for managed in list(self.devices.values()):
                if not managed.grabbed and now >= managed.next_grab_attempt:
                    self._safe_to_grab(managed)
        self._emit_grab_state()

    def _release_virtual_keys(self, managed: _ManagedDevice) -> None:
        if not managed.virtual_keys_down:
            return
        wrote_release = False
        for code in sorted(managed.virtual_keys_down):
            try:
                self.backend.release_key(managed.sink, code)
                wrote_release = True
            except OSError:
                pass
        managed.virtual_keys_down.clear()
        if wrote_release:
            try:
                self.backend.sync(managed.sink)
            except OSError:
                pass

    def _ungrab_device(self, managed: _ManagedDevice) -> None:
        # Release proxy-side keys before the real keyboard is returned.  This is
        # essential when the helper is disabled/killed between a key press and
        # release; otherwise the compositor retains a logically held key.
        self._release_virtual_keys(managed)
        if not managed.grabbed:
            return
        try:
            self.backend.ungrab(managed.source)
        except OSError:
            pass
        managed.grabbed = False
        self._emit("device", action="ungrabbed", path=managed.path)

    def _clear_trigger_state(self, *, reason: str) -> None:
        if not self.trigger_devices:
            return
        previous = sorted(self.trigger_devices)
        self.trigger_devices.clear()
        self._emit(
            "trigger",
            state="released",
            device=None,
            devices=[],
            previous_devices=previous,
            reason=reason,
        )

    def _set_trigger(self, path: str, pressed: bool) -> None:
        was_pressed = bool(self.trigger_devices)
        if pressed:
            self.trigger_devices.add(path)
        else:
            self.trigger_devices.discard(path)
        is_pressed = bool(self.trigger_devices)
        if was_pressed == is_pressed:
            return
        self._emit(
            "trigger",
            state="pressed" if is_pressed else "released",
            device=path,
            devices=sorted(self.trigger_devices),
            reason="input",
        )
        if is_pressed and not was_pressed and any(
            managed.virtual_keys_down for managed in self.devices.values()
        ):
            # A modifier or another key held before Right Ctrl can alter the
            # meaning of synthetic backspaces/paste. Tell the parent only that
            # the caret assumption is unsafe, never which key is involved.
            self._emit("activity", source="keyboard")

    def _handle_events(self, path: str, raw_events: Sequence[Any]) -> None:
        managed = self.devices.get(path)
        if managed is None or not managed.grabbed:
            return

        for event in raw_events:
            event_type = int(event.type)
            event_code = int(event.code)
            event_value = int(event.value)
            if event_type == EV_SYN and event_code == SYN_DROPPED:
                # The kernel's event buffer overflowed; all state until the
                # next SYN_REPORT is unreliable. Dropping/reopening returns the
                # physical keyboard immediately, releases proxy-side keys and
                # lets the normal all-keys-up guard resynchronize safely.
                self._emit(
                    "error",
                    code="input-syn-dropped",
                    message="kernel input event buffer overflow; device is being resynchronized",
                    path=path,
                    fatal=False,
                )
                self._drop_device(path, reason="syn-dropped")
                self.next_scan = 0.0
                return
            if event_type == EV_KEY and event_code == self.config.trigger_code:
                if event_value == 1:
                    self._set_trigger(path, True)
                elif event_value == 0:
                    self._set_trigger(path, False)
                # value=2 is key-repeat and is intentionally swallowed.
                continue

            try:
                self.backend.forward_event(managed.sink, event)
            except OSError as exc:
                self._emit(
                    "error",
                    code="input-forward-failed",
                    message=str(exc),
                    path=path,
                    fatal=False,
                )
                self._drop_device(path, reason="forward-failed")
                return

            if event_type == EV_KEY:
                if event_value == 0:
                    managed.virtual_keys_down.discard(event_code)
                elif event_value in {1, 2}:
                    managed.virtual_keys_down.add(event_code)
                if event_value == 1:
                    # Direct live text can still be awaiting its authoritative
                    # final reconciliation after Right Ctrl was released. The
                    # parent therefore needs generic activity for every
                    # physical press, but never the key code or inferred text.
                    self._emit("activity", source="keyboard")

    def handle_batch(self, batch: PollBatch) -> None:
        # Consume final release events before honoring a simultaneous udev remove.
        for path, events in batch.events.items():
            self._handle_events(path, events)
        for path in sorted(batch.removed):
            self.discovered_paths.discard(path)
            self._drop_device(path, reason="device-removed")
        if batch.rescan:
            self.next_scan = 0.0

    def _drop_device(self, path: str, *, reason: str) -> None:
        managed = self.devices.pop(path, None)
        if managed is None:
            return
        had_trigger = path in self.trigger_devices
        if had_trigger:
            self.trigger_devices.discard(path)
            if not self.trigger_devices:
                self._emit(
                    "trigger",
                    state="released",
                    device=path,
                    devices=[],
                    reason=reason,
                )
        self._ungrab_device(managed)
        self._close_quietly(managed.sink)
        self._close_quietly(managed.source)
        self._emit("device", action="removed", path=path, reason=reason)

    def _close_quietly(self, device: Any) -> None:
        try:
            self.backend.close_device(device)
        except OSError:
            pass

    def _handle_command(self, raw_command: Any) -> None:
        if isinstance(raw_command, str):
            command = raw_command
            payload: Mapping[str, Any] = {}
        elif isinstance(raw_command, Mapping):
            command = str(raw_command.get("type", ""))
            payload = raw_command
        else:
            self._emit(
                "error",
                code="invalid-control-command",
                message="control command must be a string or mapping",
                fatal=False,
            )
            return

        if command == "heartbeat":
            self.last_heartbeat = self.clock()
        elif command in {"enable", "unlock", "resume"}:
            self.set_enabled(True, reason=str(payload.get("reason", command)))
        elif command in {"disable", "lock", "suspend"}:
            self.set_enabled(False, reason=str(payload.get("reason", command)))
        elif command == "rescan":
            self.next_scan = 0.0
        elif command in {"shutdown", "stop"}:
            self.stopping = True
        else:
            self._emit(
                "error",
                code="unknown-control-command",
                message=f"unknown control command: {command!r}",
                fatal=False,
            )

    def _drain_control(self) -> None:
        if self.control is None:
            return
        while True:
            try:
                if not self.control.poll(0):
                    return
                command = self.control.recv()
            except (BrokenPipeError, EOFError, OSError):
                self.stopping = True
                return
            self._handle_command(command)

    def _enforce_heartbeat(self) -> None:
        timeout = self.config.heartbeat_timeout_seconds
        if (
            self.control is not None
            and self.enabled
            and timeout > 0
            and self.clock() - self.last_heartbeat > timeout
        ):
            self.set_enabled(False, reason="heartbeat-timeout")
            self._emit(
                "error",
                code="heartbeat-timeout",
                message="parent heartbeat timed out; input devices released",
                fatal=False,
            )

    def step(self, timeout: float | None = None) -> None:
        """Perform one bounded helper iteration (also useful in tests)."""

        self._drain_control()
        self._enforce_heartbeat()
        if self.stopping:
            return

        now = self.clock()
        if now >= self.next_scan:
            self.rescan()
            self.next_scan = now + self.config.rescan_interval_seconds
        self._reconcile_grabs()

        poll_timeout = self.config.poll_interval_seconds if timeout is None else timeout
        try:
            batch = self.backend.poll(
                {path: item.source for path, item in self.devices.items()},
                max(0.0, poll_timeout),
            )
        except OSError as exc:
            self._emit("error", code="input-poll-failed", message=str(exc), fatal=False)
            self.next_scan = 0.0
        else:
            self.handle_batch(batch)

        self._reconcile_grabs()
        self._drain_control()
        self._enforce_heartbeat()

    def run_forever(self) -> None:
        self._emit("status", state="started")
        try:
            while not self.stopping:
                self.step()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.set_enabled(False, reason="helper-exit")
        for path in list(self.devices):
            self._drop_device(path, reason="helper-exit")
        try:
            self.backend.close()
        except OSError:
            pass
        self._emit("status", state="stopped")


def input_proxy_main(
    control: Any,
    events: Any,
    config: InputProxyConfig | Mapping[str, Any] | None = None,
) -> None:
    """Top-level multiprocessing target for the input helper."""

    parsed = InputProxyConfig.from_value(config)
    try:
        backend = LinuxEvdevBackend(
            trigger_code=parsed.trigger_code,
            proxy_name_prefix=parsed.proxy_name_prefix,
        )
    except InputBackendUnavailable as exc:
        try:
            events.send(
                {
                    "type": "error",
                    "timestamp": time.monotonic(),
                    "code": "backend-unavailable",
                    "message": str(exc),
                    "fatal": True,
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        return

    service = InputProxyService(
        backend,
        control=control,
        events=events,
        config=parsed,
    )
    service.run_forever()


@dataclass(slots=True)
class InputProxyHandle:
    """Parent-side convenience wrapper returned by :func:`spawn_input_proxy`."""

    process: multiprocessing.Process
    control: Any
    events: Any

    def send(self, command: str, **fields: Any) -> None:
        self.control.send({"type": command, **fields})

    def heartbeat(self) -> None:
        self.send("heartbeat")

    def enable(self, *, reason: str = "ready") -> None:
        self.send("enable", reason=reason)

    def disable(self, *, reason: str = "parent") -> None:
        self.send("disable", reason=reason)

    def stop(self, timeout: float = 2.0) -> None:
        if self.process.is_alive():
            try:
                self.send("shutdown")
            except (BrokenPipeError, EOFError, OSError):
                pass
            self.process.join(timeout)
        if self.process.is_alive():
            # Process death also releases kernel EVIOCGRABs.  This is a bounded
            # last resort for a wedged helper, not the normal shutdown path.
            self.process.terminate()
            self.process.join(timeout)


def spawn_input_proxy(
    config: InputProxyConfig | Mapping[str, Any] | None = None,
    *,
    context: multiprocessing.context.BaseContext | None = None,
    daemon: bool = True,
) -> InputProxyHandle:
    """Start the helper using the safe ``spawn`` multiprocessing context."""

    ctx = context or multiprocessing.get_context("spawn")
    parent_control, child_control = ctx.Pipe(duplex=True)
    parent_events, child_events = ctx.Pipe(duplex=False)
    process = ctx.Process(
        target=input_proxy_main,
        args=(child_control, child_events, InputProxyConfig.from_value(config)),
        name="local-dictation-input",
        daemon=daemon,
    )
    process.start()
    child_control.close()
    child_events.close()
    return InputProxyHandle(process=process, control=parent_control, events=parent_events)


__all__ = [
    "EV_KEY",
    "EV_SYN",
    "KEY_RIGHTCTRL",
    "PROXY_NAME_PREFIX",
    "SYN_DROPPED",
    "SYN_REPORT",
    "InputBackendUnavailable",
    "InputProxyConfig",
    "InputProxyHandle",
    "InputProxyService",
    "LinuxEvdevBackend",
    "PollBatch",
    "input_proxy_main",
    "spawn_input_proxy",
]
