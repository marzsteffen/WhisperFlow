"""Private, persistent ``whisper-server`` process and HTTP client.

The server is deliberately reachable only through a random path on the IPv4
loopback interface.  Its stdout is drained and discarded: whisper-server may
write inference output there, and local-dictation must not retain transcripts
in logs.  A small, sanitized stderr ring is kept for actionable startup errors.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import requests

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_THREADS = 8

_VULKAN_BACKEND_RE = re.compile(
    r"whisper_backend_init_gpu:\s*using\s+Vulkan[^\r\n]*\sbackend\b",
    re.IGNORECASE,
)
_PORT_CONFLICT_RE = re.compile(
    r"(?:address\s+already\s+in\s+use|eaddrinuse|"
    r"could(?:\s+not|n't)\s+bind|fail(?:ed)?[^\r\n]*\bbind|"
    r"\bbind(?:ing)?[^\r\n]*(?:fail|error|couldn))",
    re.IGNORECASE,
)


class Backend(str, Enum):
    """Backends the application may explicitly request."""

    VULKAN = "vulkan"
    CPU = "cpu"


class EngineError(RuntimeError):
    """Base class for whisper engine failures safe to show to the user."""


class EngineConfigurationError(EngineError):
    """Required executable, model, or runtime configuration is unavailable."""


class EngineStartError(EngineError):
    """The server process could not become ready."""


class EngineStartTimeout(EngineStartError):
    """The server did not become ready before its startup deadline."""


class BackendUnavailableError(EngineStartError):
    """The explicitly requested inference backend could not be verified."""


class EngineProcessError(EngineError):
    """The server exited or became unavailable unexpectedly."""


class EngineCancelled(EngineError):
    """A pending engine operation was cancelled."""


class TranscriptionError(EngineError):
    """An inference request failed or returned an invalid result."""


class TranscriptionTimeout(TranscriptionError):
    """An inference request exceeded its configured timeout."""


class _PortRace(EngineStartError):
    """Internal signal that a once-free port was claimed before server bind."""


class _Response(Protocol):
    status_code: int

    def json(self) -> Any: ...

    def close(self) -> None: ...


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> _Response: ...

    def post(self, url: str, **kwargs: Any) -> _Response: ...

    def close(self) -> None: ...


class _Process(Protocol):
    stdout: Any
    stderr: Any

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Timeout = float | tuple[float, float]
CancelEvent = threading.Event
PopenFactory = Callable[..., _Process]
PortChooser = Callable[[], int]
PortAvailable = Callable[[int], bool]


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Immutable process and timeout configuration for :class:`WhisperEngine`."""

    model_path: Path | str
    vad_model_path: Path | str
    runtime_dir: Path | str
    language: str = "de"
    backend: Backend | str = Backend.VULKAN
    executable: str = "whisper-server"
    threads: int = DEFAULT_THREADS
    startup_timeout_s: float = 180.0
    startup_attempts: int = 3
    health_timeout_s: float = 0.5
    health_poll_interval_s: float = 0.1
    backend_evidence_timeout_s: float = 3.0
    transcription_connect_timeout_s: float = 2.0
    transcription_timeout_s: float = 360.0
    shutdown_timeout_s: float = 3.0
    kill_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_path", Path(self.model_path).expanduser())
        object.__setattr__(self, "vad_model_path", Path(self.vad_model_path).expanduser())
        object.__setattr__(self, "runtime_dir", Path(self.runtime_dir).expanduser())
        try:
            backend = self.backend if isinstance(self.backend, Backend) else Backend(self.backend)
        except ValueError as exc:
            raise ValueError("backend must be 'vulkan' or 'cpu'") from exc
        object.__setattr__(self, "backend", backend)

        if not self.language.strip():
            raise ValueError("language must not be empty")
        if not self.executable.strip():
            raise ValueError("executable must not be empty")
        if self.threads < 1:
            raise ValueError("threads must be at least one")
        positive = {
            "startup_timeout_s": self.startup_timeout_s,
            "startup_attempts": self.startup_attempts,
            "health_timeout_s": self.health_timeout_s,
            "health_poll_interval_s": self.health_poll_interval_s,
            "backend_evidence_timeout_s": self.backend_evidence_timeout_s,
            "transcription_connect_timeout_s": self.transcription_connect_timeout_s,
            "transcription_timeout_s": self.transcription_timeout_s,
            "shutdown_timeout_s": self.shutdown_timeout_s,
            "kill_timeout_s": self.kill_timeout_s,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @classmethod
    def from_app_config(
        cls,
        app_config: Any,
        runtime_dir: Path | str,
        **overrides: Any,
    ) -> EngineConfig:
        """Create engine settings from the application's public config object."""

        values: dict[str, Any] = {
            "model_path": app_config.model_path,
            "vad_model_path": app_config.vad_model_path,
            "runtime_dir": runtime_dir,
            "language": app_config.language,
            "backend": app_config.backend,
        }
        values.update(overrides)
        return cls(**values)


def choose_loopback_port() -> int:
    """Ask the kernel for an unused IPv4 loopback port.

    The socket must be closed before whisper-server can bind.  Consequently a
    tiny race remains; :meth:`WhisperEngine.start` detects and retries it.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


def loopback_port_available(port: int) -> bool:
    """Return whether ``port`` can currently be bound on loopback."""

    if not 1 <= port <= 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOOPBACK_HOST, port))
    except OSError:
        return False
    return True


class WhisperEngine:
    """Own a long-lived whisper-server and serialize inference requests.

    Dependencies are injectable so process behavior, HTTP responses, and port
    races can be exercised without starting a real model.  ``cancel_event`` is
    checked before and after HTTP operations; calling :meth:`stop` additionally
    terminates the server, which interrupts an in-flight request.
    """

    def __init__(
        self,
        config: EngineConfig,
        *,
        popen_factory: PopenFactory = subprocess.Popen,
        session: _Session | None = None,
        port_chooser: PortChooser = choose_loopback_port,
        port_available: PortAvailable = loopback_port_available,
        request_path_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory
        self._owns_session = session is None
        if session is None:
            private_session = requests.Session()
            # Audio and prompts target loopback only and must never be routed
            # through HTTP(S)_PROXY/ALL_PROXY inherited from the desktop.
            private_session.trust_env = False
            self._session = private_session
        else:
            self._session = session
        self._port_chooser = port_chooser
        self._port_available = port_available
        self._request_path_factory = request_path_factory or self._new_request_path
        self._monotonic = monotonic
        self._sleep = sleep

        self._process: _Process | None = None
        self._port: int | None = None
        self._request_path: str | None = None
        self._ready = False
        self._backend_verified = False
        self._closed = False

        self._lifecycle_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._diagnostic_lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._vulkan_seen = threading.Event()
        self._diagnostics: deque[str] = deque(maxlen=80)
        self._drain_threads: list[threading.Thread] = []
        self._sensitive_paths: deque[str] = deque(maxlen=64)

    @staticmethod
    def _new_request_path() -> str:
        return "/" + secrets.token_urlsafe(24)

    @property
    def ready(self) -> bool:
        process = self._process
        return self._ready and process is not None and process.poll() is None

    @property
    def backend(self) -> Backend:
        return self.config.backend  # type: ignore[return-value]

    @property
    def backend_verified(self) -> bool:
        return self.ready and self._backend_verified

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def request_path(self) -> str | None:
        return self._request_path

    @property
    def base_url(self) -> str | None:
        if self._port is None or self._request_path is None:
            return None
        return f"http://{LOOPBACK_HOST}:{self._port}{self._request_path}"

    @property
    def inference_url(self) -> str | None:
        base = self.base_url
        return f"{base}/inference" if base is not None else None

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return the bounded, sanitized in-memory stderr diagnostics."""

        with self._diagnostic_lock:
            return tuple(self._diagnostics)

    def _validate_files(self) -> None:
        if not self.config.model_path.is_file():
            raise EngineConfigurationError(
                f"Whisper-Modell fehlt: {self.config.model_path.name}"
            )
        if not self.config.vad_model_path.is_file():
            raise EngineConfigurationError(
                f"VAD-Modell fehlt: {self.config.vad_model_path.name}"
            )
        try:
            self.config.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise EngineConfigurationError(
                "Das private Laufzeitverzeichnis kann nicht erstellt werden"
            ) from exc
        if not self.config.runtime_dir.is_dir():
            raise EngineConfigurationError("Das Laufzeitverzeichnis ist kein Verzeichnis")

    def _build_command(self, port: int, request_path: str) -> list[str]:
        command = [
            self.config.executable,
            "--model",
            os.fspath(self.config.model_path),
            "--vad-model",
            os.fspath(self.config.vad_model_path),
            "--host",
            LOOPBACK_HOST,
            "--port",
            str(port),
            "--request-path",
            request_path,
            "--tmp-dir",
            os.fspath(self.config.runtime_dir),
            "--language",
            self.config.language,
            "--threads",
            str(self.config.threads),
            "--vad",
            "--no-timestamps",
            "--flash-attn",
        ]
        if self.backend is Backend.CPU:
            command.append("--no-gpu")
        return command

    def start(
        self,
        *,
        warmup_wav: Path | str | None = None,
        cancel_event: CancelEvent | None = None,
    ) -> None:
        """Start the server, verify its backend, and optionally warm both models."""

        with self._lifecycle_lock:
            if self._closed:
                raise EngineError("Die Whisper-Engine wurde bereits geschlossen")
            if self.ready:
                return
            self._cancel_requested.clear()
            self._validate_files()
            self._terminate_current()

            saw_unavailable_port = False
            last_port_error: _PortRace | None = None
            for attempt in range(self.config.startup_attempts):
                self._raise_if_cancelled(cancel_event)
                try:
                    port = int(self._port_chooser())
                except (OSError, TypeError, ValueError) as exc:
                    raise EngineStartError("Kein lokaler Server-Port verfügbar") from exc
                if not self._port_available(port):
                    saw_unavailable_port = True
                    continue

                request_path = self._normalize_request_path(self._request_path_factory())
                self._reset_attempt_state(port, request_path)
                command = self._build_command(port, request_path)
                try:
                    process = self._popen_factory(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        close_fds=True,
                        start_new_session=os.name != "nt",
                        bufsize=0,
                    )
                except FileNotFoundError as exc:
                    self._clear_endpoint()
                    raise EngineConfigurationError(
                        "whisper-server ist nicht installiert oder nicht im PATH"
                    ) from exc
                except OSError as exc:
                    self._clear_endpoint()
                    raise EngineStartError("whisper-server konnte nicht gestartet werden") from exc

                self._process = process
                self._start_drainers(process)
                try:
                    self._wait_until_ready(process, cancel_event)
                    self._verify_backend(process, cancel_event)
                    self._ready = True
                    self._backend_verified = True
                    if warmup_wav is not None:
                        self._request_transcription(
                            warmup_wav,
                            initial_prompt="",
                            timeout=None,
                            cancel_event=cancel_event,
                        )
                    return
                except _PortRace as exc:
                    last_port_error = exc
                    self._terminate_current()
                    if attempt + 1 < self.config.startup_attempts:
                        continue
                    break
                except Exception:
                    self._terminate_current()
                    raise

            self._clear_endpoint()
            if last_port_error is not None or saw_unavailable_port:
                raise EngineStartError(
                    "Ein privater lokaler Server-Port konnte nicht zuverlässig reserviert werden"
                ) from last_port_error
            raise EngineStartError("whisper-server konnte nicht gestartet werden")

    @staticmethod
    def _normalize_request_path(value: str) -> str:
        path = value.strip()
        if not path:
            raise EngineConfigurationError("Der private Serverpfad darf nicht leer sein")
        if not path.startswith("/"):
            path = "/" + path
        if path == "/" or "/" in path[1:] or not re.fullmatch(r"/[A-Za-z0-9_-]+", path):
            raise EngineConfigurationError("Der private Serverpfad ist ungültig")
        return path

    def _reset_attempt_state(self, port: int, request_path: str) -> None:
        self._ready = False
        self._backend_verified = False
        self._vulkan_seen.clear()
        self._port = port
        self._request_path = request_path
        with self._diagnostic_lock:
            self._diagnostics.clear()
        self._drain_threads.clear()
        self._remember_sensitive_path(self.config.model_path)
        self._remember_sensitive_path(self.config.vad_model_path)
        self._remember_sensitive_path(self.config.runtime_dir)
        self._remember_sensitive_path(request_path)

    def _start_drainers(self, process: _Process) -> None:
        stdout_thread = threading.Thread(
            target=self._drain_stream,
            args=(process.stdout, False),
            name="whisper-server-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain_stream,
            args=(process.stderr, True),
            name="whisper-server-stderr",
            daemon=True,
        )
        self._drain_threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def _drain_stream(self, stream: Any, keep_diagnostics: bool) -> None:
        if stream is None:
            return
        while True:
            try:
                chunk = stream.readline()
            except (OSError, ValueError):
                return
            if chunk in (b"", "", None):
                return
            if not keep_diagnostics:
                # This stream can contain a recognized transcript.  Never retain it.
                continue
            if isinstance(chunk, bytes):
                line = chunk.decode("utf-8", errors="replace")
            else:
                line = str(chunk)
            if _VULKAN_BACKEND_RE.search(line):
                self._vulkan_seen.set()
            sanitized = self._sanitize_diagnostic(line)
            if sanitized:
                with self._diagnostic_lock:
                    self._diagnostics.append(sanitized)

    def _sanitize_diagnostic(self, line: str) -> str:
        text = "".join(
            char if (char >= " " and char != "\x7f") else " " for char in line
        ).strip()
        if not text:
            return ""
        with self._diagnostic_lock:
            sensitive = tuple(self._sensitive_paths)
        for path in sorted(sensitive, key=len, reverse=True):
            if path:
                text = text.replace(path, "<private-path>")
        # Bound attacker- or library-controlled output retained in memory.
        return text[:500]

    def _remember_sensitive_path(self, path: Path | str) -> None:
        raw = os.fspath(path)
        candidates = (raw, os.path.abspath(raw))
        with self._diagnostic_lock:
            for candidate in candidates:
                if candidate and candidate not in self._sensitive_paths:
                    self._sensitive_paths.append(candidate)

    def _wait_until_ready(
        self,
        process: _Process,
        cancel_event: CancelEvent | None,
    ) -> None:
        deadline = self._monotonic() + self.config.startup_timeout_s
        while self._monotonic() < deadline:
            self._raise_if_cancelled(cancel_event)
            return_code = process.poll()
            if return_code is not None:
                self._join_drainers(0.1)
                if self._port_conflict_detected():
                    raise _PortRace("whisper-server konnte den ausgewählten Port nicht binden")
                raise EngineProcessError(
                    f"whisper-server wurde beim Start unerwartet beendet (Code {return_code})"
                )
            if self._health_request(self.config.health_timeout_s):
                if process.poll() is not None:
                    continue
                return
            self._interruptible_sleep(self.config.health_poll_interval_s, cancel_event)

        self._join_drainers(0.05)
        if self._port_conflict_detected():
            raise _PortRace("Der lokale Server-Port wurde während des Starts belegt")
        raise EngineStartTimeout("whisper-server wurde nicht rechtzeitig bereit")

    def _verify_backend(
        self,
        process: _Process,
        cancel_event: CancelEvent | None,
    ) -> None:
        if self.backend is Backend.CPU:
            return
        deadline = self._monotonic() + self.config.backend_evidence_timeout_s
        while self._monotonic() < deadline:
            self._raise_if_cancelled(cancel_event)
            if self._vulkan_seen.is_set():
                return
            return_code = process.poll()
            if return_code is not None:
                raise EngineProcessError(
                    f"whisper-server wurde bei der Backend-Prüfung beendet (Code {return_code})"
                )
            self._interruptible_sleep(
                min(0.05, max(0.0, deadline - self._monotonic())),
                cancel_event,
            )
        if not self._vulkan_seen.is_set():
            raise BackendUnavailableError(
                "Vulkan konnte nicht eindeutig aktiviert werden; CPU-Fallback wurde nicht verwendet"
            )

    def _port_conflict_detected(self) -> bool:
        return any(_PORT_CONFLICT_RE.search(line) for line in self.diagnostics)

    def _health_request(self, timeout: float) -> bool:
        base = self.base_url
        if base is None:
            return False
        response: _Response | None = None
        try:
            response = self._session.get(f"{base}/health", timeout=timeout)
            if response.status_code != 200:
                return False
            payload = response.json()
            return isinstance(payload, Mapping) and payload.get("status") == "ok"
        except (requests.RequestException, OSError, ValueError, TypeError):
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def health(self, *, timeout: float | None = None) -> bool:
        """Probe the private health endpoint without exposing it publicly."""

        if not self.ready:
            return False
        with self._request_lock:
            return self._health_request(timeout or self.config.health_timeout_s)

    def transcribe(
        self,
        audio_path: Path | str,
        *,
        initial_prompt: str = "",
        timeout: Timeout | None = None,
        cancel_event: CancelEvent | None = None,
    ) -> str:
        """Transcribe one PCM WAV and return the server's unmodified ``text`` value."""

        with self._request_lock:
            self._cancel_requested.clear()
            return self._request_transcription(
                audio_path,
                initial_prompt=initial_prompt,
                timeout=timeout,
                cancel_event=cancel_event,
            )

    def warmup(
        self,
        wav_path: Path | str,
        *,
        timeout: Timeout | None = None,
        cancel_event: CancelEvent | None = None,
    ) -> str:
        """Warm the main and VAD models using the same private inference route."""

        return self.transcribe(
            wav_path,
            initial_prompt="",
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def _request_transcription(
        self,
        audio_path: Path | str,
        *,
        initial_prompt: str,
        timeout: Timeout | None,
        cancel_event: CancelEvent | None,
    ) -> str:
        self._raise_if_cancelled(cancel_event)
        if not self.ready:
            raise EngineProcessError("whisper-server ist nicht bereit")
        path = Path(audio_path).expanduser()
        if not path.is_file():
            raise TranscriptionError(f"Audiodatei fehlt: {path.name}")
        self._remember_sensitive_path(path)
        request_timeout = self._request_timeout(timeout)
        endpoint = self.inference_url
        if endpoint is None:
            raise EngineProcessError("Der private Inferenz-Endpunkt fehlt")

        fields = {
            "language": self.config.language,
            "translate": "false",
            "detect_language": "false",
            "prompt": initial_prompt,
            "carry_initial_prompt": "true",
            "vad": "true",
            "no_timestamps": "true",
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "response_format": "json",
        }
        response: _Response | None = None
        try:
            with path.open("rb") as audio:
                response = self._session.post(
                    endpoint,
                    data=fields,
                    files={"file": (path.name, audio, "audio/wav")},
                    timeout=request_timeout,
                )
            self._raise_if_cancelled(cancel_event)
            if response.status_code != 200:
                raise TranscriptionError(
                    f"whisper-server hat die Anfrage abgelehnt (HTTP {response.status_code})"
                )
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise TranscriptionError(
                    "whisper-server hat keine gültige JSON-Antwort geliefert"
                ) from None
            if not isinstance(payload, Mapping) or not isinstance(payload.get("text"), str):
                raise TranscriptionError(
                    "In der Antwort von whisper-server fehlt das Textfeld"
                )
            return payload["text"]
        except requests.Timeout:
            if self._is_cancelled(cancel_event):
                raise EngineCancelled("Die Transkription wurde abgebrochen") from None
            raise TranscriptionTimeout("Die Transkription hat das Zeitlimit überschritten") from None
        except requests.RequestException:
            if self._is_cancelled(cancel_event):
                raise EngineCancelled("Die Transkription wurde abgebrochen") from None
            if not self.ready:
                raise EngineProcessError("Die Whisper-Engine ist nicht mehr erreichbar") from None
            raise TranscriptionError("Die lokale Whisper-Anfrage ist fehlgeschlagen") from None
        except OSError:
            raise TranscriptionError("Die Audiodatei konnte nicht gelesen werden") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _request_timeout(self, timeout: Timeout | None) -> tuple[float, float]:
        if timeout is None:
            return (
                self.config.transcription_connect_timeout_s,
                self.config.transcription_timeout_s,
            )
        if isinstance(timeout, tuple):
            if len(timeout) != 2 or timeout[0] <= 0 or timeout[1] <= 0:
                raise ValueError("timeout values must be greater than zero")
            return (float(timeout[0]), float(timeout[1]))
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        return (self.config.transcription_connect_timeout_s, float(timeout))

    def cancel_pending(self) -> None:
        """Request cancellation of startup or the current inference operation."""

        self._cancel_requested.set()

    def _is_cancelled(self, cancel_event: CancelEvent | None) -> bool:
        return self._cancel_requested.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        )

    def _raise_if_cancelled(self, cancel_event: CancelEvent | None) -> None:
        if self._is_cancelled(cancel_event):
            raise EngineCancelled("Der Engine-Vorgang wurde abgebrochen")

    def _interruptible_sleep(
        self,
        duration: float,
        cancel_event: CancelEvent | None,
    ) -> None:
        if duration <= 0:
            return
        # External threading.Events can wake immediately.  The injected sleep
        # remains useful for deterministic tests and for the internal token.
        if cancel_event is not None and cancel_event.wait(duration):
            self._raise_if_cancelled(cancel_event)
        if self._cancel_requested.wait(0):
            self._raise_if_cancelled(cancel_event)
        if cancel_event is None:
            self._sleep(duration)
        self._raise_if_cancelled(cancel_event)

    def restart(
        self,
        *,
        warmup_wav: Path | str | None = None,
        cancel_event: CancelEvent | None = None,
    ) -> None:
        """Gracefully replace the server and verify the new process."""

        self.stop()
        self.start(warmup_wav=warmup_wav, cancel_event=cancel_event)

    def stop(self) -> None:
        """Cancel pending work and stop whisper-server with bounded escalation."""

        self._cancel_requested.set()
        with self._lifecycle_lock:
            self._terminate_current()

    def _terminate_current(self) -> None:
        process, self._process = self._process, None
        self._ready = False
        self._backend_verified = False
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except (OSError, ProcessLookupError, ValueError):
                pass
            if not self._wait_process(process, self.config.shutdown_timeout_s):
                try:
                    process.terminate()
                except (OSError, ProcessLookupError):
                    pass
                if not self._wait_process(process, self.config.kill_timeout_s):
                    try:
                        process.kill()
                    except (OSError, ProcessLookupError):
                        pass
                    self._wait_process(process, self.config.kill_timeout_s)
        self._join_drainers(0.2)
        self._clear_endpoint()

    @staticmethod
    def _wait_process(process: _Process, timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except (subprocess.TimeoutExpired, TimeoutError):
            return process.poll() is not None
        except (OSError, ProcessLookupError):
            return True

    def _join_drainers(self, timeout: float) -> None:
        if not self._drain_threads:
            return
        deadline = time.monotonic() + max(timeout, 0.0)
        for thread in tuple(self._drain_threads):
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)

    def _clear_endpoint(self) -> None:
        self._port = None
        self._request_path = None

    def close(self) -> None:
        """Stop the child and release HTTP resources.  Idempotent."""

        if self._closed:
            return
        self.stop()
        if self._owns_session:
            try:
                self._session.close()
            except Exception:
                pass
        self._closed = True

    def __enter__(self) -> WhisperEngine:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "Backend",
    "BackendUnavailableError",
    "EngineCancelled",
    "EngineConfig",
    "EngineConfigurationError",
    "EngineError",
    "EngineProcessError",
    "EngineStartError",
    "EngineStartTimeout",
    "TranscriptionError",
    "TranscriptionTimeout",
    "WhisperEngine",
    "choose_loopback_port",
    "loopback_port_available",
]
