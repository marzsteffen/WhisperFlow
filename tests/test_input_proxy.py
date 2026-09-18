from __future__ import annotations

import errno
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from local_dictation.input_proxy import (
    EV_KEY,
    EV_SYN,
    KEY_RIGHTCTRL,
    SYN_DROPPED,
    InputProxyConfig,
    InputProxyService,
    LinuxEvdevBackend,
    PollBatch,
)


@dataclass(slots=True)
class FakeEvent:
    type: int
    code: int
    value: int


@dataclass(slots=True)
class FakeSource:
    path: str
    name: str = "Physical keyboard"
    physical_keys: set[int] = field(default_factory=set)
    grabbed: bool = False
    closed: bool = False
    queued: list[FakeEvent] = field(default_factory=list)


@dataclass(slots=True)
class FakeSink:
    name: str
    forwarded: list[FakeEvent] = field(default_factory=list)
    releases: list[int] = field(default_factory=list)
    sync_count: int = 0
    closed: bool = False


class FakeBackend:
    def __init__(self, *paths: str) -> None:
        self.available = set(paths)
        self.sources = {path: FakeSource(path) for path in paths}
        self.sinks: dict[str, FakeSink] = {}
        self.created: list[str] = []
        self.batches: deque[PollBatch] = deque()
        self.closed = False
        self.grab_error: OSError | None = None
        self.create_errors: dict[str, OSError] = {}

    def discover_paths(self) -> set[str]:
        return set(self.available)

    def open_device(self, path: str) -> FakeSource:
        return self.sources.setdefault(path, FakeSource(path))

    def create_proxy(self, source: FakeSource, name: str) -> FakeSink:
        if error := self.create_errors.get(source.path):
            raise error
        sink = FakeSink(name)
        self.sinks[source.path] = sink
        self.created.append(source.path)
        return sink

    def active_keys(self, source: FakeSource) -> set[int]:
        return set(source.physical_keys)

    def drain_events(self, source: FakeSource) -> None:
        source.queued.clear()

    def grab(self, source: FakeSource) -> None:
        if self.grab_error is not None:
            raise self.grab_error
        source.grabbed = True

    def ungrab(self, source: FakeSource) -> None:
        source.grabbed = False

    def forward_event(self, sink: FakeSink, event: FakeEvent) -> None:
        sink.forwarded.append(event)

    def release_key(self, sink: FakeSink, code: int) -> None:
        sink.releases.append(code)

    def sync(self, sink: FakeSink) -> None:
        sink.sync_count += 1

    def close_device(self, device: FakeSource | FakeSink) -> None:
        device.closed = True

    def poll(self, devices: dict[str, FakeSource], timeout: float) -> PollBatch:
        if self.batches:
            return self.batches.popleft()
        return PollBatch()

    def close(self) -> None:
        self.closed = True


class FakeControl:
    def __init__(self) -> None:
        self.commands: deque[Any] = deque()

    def poll(self, timeout: float) -> bool:
        return bool(self.commands)

    def recv(self) -> Any:
        return self.commands.popleft()


class FakeClock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_service(
    backend: FakeBackend,
    *,
    control: FakeControl | None = None,
    clock: FakeClock | None = None,
    heartbeat_timeout: float = 2.5,
) -> tuple[InputProxyService, list[dict[str, Any]]]:
    emitted: list[dict[str, Any]] = []
    service = InputProxyService(
        backend,
        control=control,
        events=emitted.append,
        clock=clock or FakeClock(),
        config=InputProxyConfig(
            heartbeat_timeout_seconds=heartbeat_timeout,
            poll_interval_seconds=0,
            rescan_interval_seconds=60,
        ),
    )
    service.rescan()
    return service, emitted


def trigger_states(messages: list[dict[str, Any]]) -> list[str]:
    return [item["state"] for item in messages if item["type"] == "trigger"]


def test_forwards_complete_frames_but_filters_trigger_and_repeat() -> None:
    path = "/dev/input/event4"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)

    events = [
        FakeEvent(EV_KEY, 30, 1),
        FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1),
        FakeEvent(EV_KEY, KEY_RIGHTCTRL, 2),
        FakeEvent(EV_SYN, 0, 0),
        FakeEvent(EV_KEY, 30, 0),
        FakeEvent(EV_KEY, KEY_RIGHTCTRL, 0),
        FakeEvent(EV_SYN, 0, 0),
    ]
    service.handle_batch(PollBatch(events={path: events}))

    assert backend.sinks[path].forwarded == [events[0], events[3], events[4], events[6]]
    assert trigger_states(messages) == ["pressed", "released"]
    assert not service.trigger_devices


def test_trigger_is_aggregate_across_multiple_keyboards() -> None:
    first = "/dev/input/event1"
    second = "/dev/input/event2"
    backend = FakeBackend(first, second)
    service, messages = make_service(backend)
    service.set_enabled(True)

    service.handle_batch(
        PollBatch(events={first: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1)]})
    )
    service.handle_batch(
        PollBatch(events={second: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1)]})
    )
    service.handle_batch(
        PollBatch(events={first: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 0)]})
    )
    assert trigger_states(messages) == ["pressed"]
    assert service.trigger_devices == {second}

    service.handle_batch(
        PollBatch(events={second: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 0)]})
    )
    assert trigger_states(messages) == ["pressed", "released"]


def test_non_trigger_press_during_dictation_reports_only_generic_activity() -> None:
    path = "/dev/input/event3"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)

    service.handle_batch(
        PollBatch(
            events={
                path: [
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1),
                    FakeEvent(EV_KEY, 30, 1),
                    FakeEvent(EV_KEY, 30, 2),
                    FakeEvent(EV_KEY, 30, 0),
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 0),
                ]
            }
        )
    )

    activity = [item for item in messages if item["type"] == "activity"]
    assert len(activity) == 1
    assert activity[0]["source"] == "keyboard"
    assert "code" not in activity[0]
    assert backend.sinks[path].forwarded[0].code == 30


def test_non_trigger_press_after_release_still_reports_generic_activity() -> None:
    path = "/dev/input/event4"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)

    service.handle_batch(
        PollBatch(
            events={
                path: [
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1),
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 0),
                    FakeEvent(EV_KEY, 30, 1),
                    FakeEvent(EV_KEY, 30, 0),
                ]
            }
        )
    )

    activity = [item for item in messages if item["type"] == "activity"]
    assert len(activity) == 1
    assert set(activity[0]) == {"type", "timestamp", "source"}


def test_key_held_before_trigger_also_reports_generic_activity() -> None:
    path = "/dev/input/event5"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)

    service.handle_batch(
        PollBatch(
            events={
                path: [
                    FakeEvent(EV_KEY, 42, 1),
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1),
                ]
            }
        )
    )

    activity = [item for item in messages if item["type"] == "activity"]
    assert len(activity) == 2
    assert all(set(item) == {"type", "timestamp", "source"} for item in activity)


def test_waits_for_all_physical_keys_to_be_up_before_grab() -> None:
    path = "/dev/input/event7"
    backend = FakeBackend(path)
    backend.sources[path].physical_keys.add(42)
    service, _ = make_service(backend)

    service.set_enabled(True)
    assert service.enabled
    assert not backend.sources[path].grabbed

    backend.sources[path].physical_keys.clear()
    service.step(timeout=0)
    assert backend.sources[path].grabbed


def test_disable_releases_virtual_keys_before_ungrab() -> None:
    path = "/dev/input/event8"
    backend = FakeBackend(path)
    service, _ = make_service(backend)
    service.set_enabled(True)
    service.handle_batch(
        PollBatch(
            events={
                path: [
                    FakeEvent(EV_KEY, 42, 1),
                    FakeEvent(EV_KEY, 30, 1),
                    FakeEvent(EV_SYN, 0, 0),
                ]
            }
        )
    )

    service.set_enabled(False, reason="screen-lock")

    sink = backend.sinks[path]
    assert sink.releases == [30, 42]
    assert sink.sync_count == 1
    assert not backend.sources[path].grabbed
    assert not service.devices[path].virtual_keys_down


def test_disable_while_trigger_down_emits_release() -> None:
    path = "/dev/input/event9"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)
    service.handle_batch(
        PollBatch(events={path: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1)]})
    )

    service.set_enabled(False, reason="suspend")

    assert trigger_states(messages) == ["pressed", "released"]
    release = [item for item in messages if item.get("state") == "released"][-1]
    assert release["reason"] == "suspend"
    assert not backend.sources[path].grabbed


def test_heartbeat_timeout_disables_and_releases() -> None:
    path = "/dev/input/event10"
    backend = FakeBackend(path)
    control = FakeControl()
    clock = FakeClock()
    service, messages = make_service(backend, control=control, clock=clock)
    service.set_enabled(True)
    service.handle_batch(
        PollBatch(events={path: [FakeEvent(EV_KEY, 29, 1)]})
    )

    clock.advance(2.6)
    service.step(timeout=0)

    assert not service.enabled
    assert not backend.sources[path].grabbed
    assert backend.sinks[path].releases == [29]
    assert any(item.get("code") == "heartbeat-timeout" for item in messages)


def test_control_commands_cover_lock_resume_and_shutdown() -> None:
    path = "/dev/input/event11"
    backend = FakeBackend(path)
    control = FakeControl()
    service, _ = make_service(backend, control=control, heartbeat_timeout=30)

    control.commands.extend(["enable", {"type": "heartbeat"}, "lock"])
    service.step(timeout=0)
    assert not service.enabled
    assert not backend.sources[path].grabbed

    control.commands.append("resume")
    service.step(timeout=0)
    assert service.enabled
    assert backend.sources[path].grabbed

    control.commands.append("shutdown")
    service.step(timeout=0)
    assert service.stopping


def test_hotplug_add_and_remove_uses_one_proxy_and_clears_trigger() -> None:
    path = "/dev/input/event12"
    backend = FakeBackend()
    service, messages = make_service(backend)
    service.set_enabled(True)

    backend.available.add(path)
    backend.sources[path] = FakeSource(path)
    service.handle_batch(PollBatch(rescan=True))
    service.step(timeout=0)
    assert backend.created == [path]
    assert backend.sources[path].grabbed

    service.handle_batch(
        PollBatch(events={path: [FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1)]})
    )
    backend.available.remove(path)
    service.handle_batch(PollBatch(removed={path}, rescan=True))

    assert path not in service.devices
    assert backend.sinks[path].closed
    assert backend.sources[path].closed
    assert trigger_states(messages) == ["pressed", "released"]


def test_syn_dropped_releases_proxy_state_and_resynchronizes_device() -> None:
    path = "/dev/input/event19"
    backend = FakeBackend(path)
    service, messages = make_service(backend)
    service.set_enabled(True)
    service.handle_batch(
        PollBatch(
            events={
                path: [
                    FakeEvent(EV_KEY, 56, 1),
                    FakeEvent(EV_KEY, KEY_RIGHTCTRL, 1),
                    FakeEvent(EV_SYN, SYN_DROPPED, 0),
                    FakeEvent(EV_KEY, 30, 1),
                ]
            }
        )
    )

    assert path not in service.devices
    assert backend.sinks[path].releases == [56]
    assert not backend.sources[path].grabbed
    assert backend.sources[path].closed
    assert backend.sinks[path].closed
    assert trigger_states(messages) == ["pressed", "released"]
    assert any(item.get("code") == "input-syn-dropped" for item in messages)
    assert service.next_scan == 0.0


def test_enodev_during_grab_drops_device() -> None:
    path = "/dev/input/event13"
    backend = FakeBackend(path)
    backend.grab_error = OSError(errno.ENODEV, "gone")
    service, _ = make_service(backend)

    service.set_enabled(True)

    assert path not in service.devices
    assert backend.sources[path].closed
    assert backend.sinks[path].closed


def test_busy_grab_is_reported_rate_limited_and_marks_partial() -> None:
    path = "/dev/input/event15"
    backend = FakeBackend(path)
    backend.grab_error = OSError(errno.EBUSY, "busy")
    clock = FakeClock()
    service, messages = make_service(backend, clock=clock)

    service.set_enabled(True)

    grab_errors = [item for item in messages if item.get("code") == "input-grab-failed"]
    assert len(grab_errors) == 1
    assert grab_errors[0]["errno"] == errno.EBUSY
    assert grab_errors[0]["retrying"] is True
    status = [item for item in messages if item.get("state") == "input-partial"][-1]
    assert status == {
        "type": "status",
        "timestamp": clock(),
        "state": "input-partial",
        "discovered": 1,
        "grabbed": 0,
        "all_grabbed": False,
    }

    clock.advance(1.0)
    service.step(timeout=0)
    assert len([item for item in messages if item.get("code") == "input-grab-failed"]) == 1

    clock.advance(5.0)
    service.step(timeout=0)
    assert len([item for item in messages if item.get("code") == "input-grab-failed"]) == 2


def test_successful_reconciliation_emits_positive_readiness() -> None:
    path = "/dev/input/event16"
    backend = FakeBackend(path)
    service, messages = make_service(backend)

    service.set_enabled(True)

    ready = [item for item in messages if item.get("state") == "input-ready"][-1]
    assert ready["discovered"] == 1
    assert ready["grabbed"] == 1
    assert ready["all_grabbed"] is True


def test_failed_proxy_creation_remains_in_readiness_denominator() -> None:
    first = "/dev/input/event17"
    second = "/dev/input/event18"
    backend = FakeBackend(first, second)
    backend.create_errors[second] = PermissionError(errno.EACCES, "denied")
    service, messages = make_service(backend)

    service.set_enabled(True)

    partial = [item for item in messages if item.get("state") == "input-partial"][-1]
    assert partial["discovered"] == 2
    assert partial["grabbed"] == 1
    assert partial["all_grabbed"] is False


def test_close_is_idempotent_and_releases_everything() -> None:
    path = "/dev/input/event14"
    backend = FakeBackend(path)
    service, _ = make_service(backend)
    service.set_enabled(True)
    service.handle_batch(
        PollBatch(events={path: [FakeEvent(EV_KEY, 56, 1)]})
    )

    service.close()
    service.close()

    assert backend.sinks[path].releases == [56]
    assert backend.sources[path].closed
    assert backend.sinks[path].closed
    assert backend.closed


class FakeUdevDevice:
    def __init__(self, node: str, sys_path: str, action: str | None = None) -> None:
        self.device_node = node
        self.sys_path = sys_path
        self.action = action
        self.properties = {"DEVNAME": node}


class FakeEvdevInput:
    records: ClassVar[dict[str, tuple[str, set[int]]]] = {}

    def __init__(self, path: str) -> None:
        self.path = path
        self.name, self.keys = self.records[path]
        self.closed = False

    def capabilities(self, verbose: bool, absinfo: bool) -> dict[int, list[int]]:
        return {EV_KEY: list(self.keys)}

    def close(self) -> None:
        self.closed = True


class FakeMonitor:
    @classmethod
    def from_netlink(cls, context: Any) -> FakeMonitor:
        return cls()

    def filter_by(self, subsystem: str) -> None:
        assert subsystem == "input"

    def start(self) -> None:
        pass

    def fileno(self) -> int:
        return 99

    def poll(self, timeout: int) -> None:
        return None


def test_linux_discovery_excludes_virtual_owned_and_incapable_devices() -> None:
    physical = "/dev/input/event1"
    virtual = "/dev/input/event2"
    owned = "/dev/input/event3"
    mouse = "/dev/input/event4"
    devices = [
        FakeUdevDevice(physical, "/sys/devices/pci0000:00/input/input1/event1"),
        FakeUdevDevice(virtual, "/sys/devices/virtual/input/input2/event2"),
        FakeUdevDevice(owned, "/sys/devices/pci0000:00/input/input3/event3"),
        FakeUdevDevice(mouse, "/sys/devices/pci0000:00/input/input4/event4"),
    ]
    FakeEvdevInput.records = {
        physical: ("Keychron", {KEY_RIGHTCTRL, 30}),
        virtual: ("Other virtual keyboard", {KEY_RIGHTCTRL}),
        owned: ("LOCAL-DICTATION PROXY event0", {KEY_RIGHTCTRL}),
        mouse: ("Mouse", {272}),
    }
    context = SimpleNamespace(list_devices=lambda subsystem: devices)
    pyudev_module = SimpleNamespace(
        Context=lambda: context,
        Monitor=FakeMonitor,
    )
    evdev_module = SimpleNamespace(InputDevice=FakeEvdevInput, UInput=object())

    backend = LinuxEvdevBackend(
        evdev_module=evdev_module,
        pyudev_module=pyudev_module,
    )

    assert backend.discover_paths() == {physical}


def test_import_does_not_require_evdev_or_pyudev() -> None:
    # Reaching this test already proves module import did not require either
    # optional dependency.  It also guards the stable ABI value used by config.
    assert KEY_RIGHTCTRL == 97


def test_config_rejects_unknown_process_settings() -> None:
    with pytest.raises(ValueError, match="unknown input proxy setting"):
        InputProxyConfig.from_value({"surprise": True})
