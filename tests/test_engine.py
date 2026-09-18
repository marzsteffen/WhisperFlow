from __future__ import annotations

import io
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests

from local_dictation.engine import (
    Backend,
    BackendUnavailableError,
    EngineCancelled,
    EngineConfig,
    EngineConfigurationError,
    EngineProcessError,
    EngineStartError,
    TranscriptionError,
    TranscriptionTimeout,
    WhisperEngine,
)

VULKAN_LOG = b"whisper_backend_init_gpu: using Vulkan0 backend\n"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, json_error=None):
        self.status_code = status_code
        self.payload = {"status": "ok"} if payload is None else payload
        self.json_error = json_error
        self.closed = False

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, health=None, transcription=None):
        self.health = deque(health or [FakeResponse()])
        self.transcription = transcription or FakeResponse(payload={"text": "Hallo Welt."})
        self.get_calls = []
        self.post_calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        value = self.health[0] if len(self.health) == 1 else self.health.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def post(self, url, **kwargs):
        file_part = kwargs["files"]["file"]
        self.post_calls.append(
            {
                "url": url,
                "data": dict(kwargs["data"]),
                "filename": file_part[0],
                "audio": file_part[1].read(),
                "mime": file_part[2],
                "timeout": kwargs["timeout"],
            }
        )
        if isinstance(self.transcription, BaseException):
            raise self.transcription
        return self.transcription

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, *, stderr=b"", stdout=b"", returncode=None, stubborn=False):
        self.stderr = io.BytesIO(stderr)
        self.stdout = io.BytesIO(stdout)
        self.returncode = returncode
        self.stubborn = stubborn
        self.signals = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if not self.stubborn:
            self.returncode = 0

    def terminate(self):
        self.terminate_calls += 1
        if not self.stubborn:
            self.returncode = -15

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("whisper-server", timeout)
        return self.returncode


class FakePopen:
    def __init__(self, *processes):
        self.processes = deque(processes)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return self.processes.popleft()


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.model = self.root / "ggml-large-v3-turbo.bin"
        self.vad = self.root / "ggml-silero-v6.2.0.bin"
        self.wav = self.root / "speech.wav"
        self.model.write_bytes(b"model")
        self.vad.write_bytes(b"vad")
        self.wav.write_bytes(b"RIFFtest")
        self.runtime = self.root / "runtime"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_internal_http_session_never_uses_environment_proxies(self):
        engine = WhisperEngine(self.config())

        self.assertFalse(engine._session.trust_env)

        engine.close()

    def config(self, **overrides):
        values = {
            "model_path": self.model,
            "vad_model_path": self.vad,
            "runtime_dir": self.runtime,
            "startup_timeout_s": 0.2,
            "startup_attempts": 3,
            "health_timeout_s": 0.01,
            "health_poll_interval_s": 0.005,
            "backend_evidence_timeout_s": 0.05,
            "transcription_connect_timeout_s": 0.25,
            "transcription_timeout_s": 5.0,
            "shutdown_timeout_s": 0.01,
            "kill_timeout_s": 0.01,
        }
        values.update(overrides)
        return EngineConfig(**values)

    def engine(
        self,
        *,
        process=None,
        popen=None,
        session=None,
        config=None,
        ports=None,
        path="/private-token",
    ):
        process = process or FakeProcess(stderr=VULKAN_LOG)
        popen = popen or FakePopen(process)
        port_values = iter(ports or [45123])
        result = WhisperEngine(
            config or self.config(),
            popen_factory=popen,
            session=session or FakeSession(),
            port_chooser=lambda: next(port_values),
            port_available=lambda _port: True,
            request_path_factory=lambda: path,
        )
        return result, popen, process

    def test_vulkan_start_builds_private_hardened_command(self):
        process = FakeProcess(
            stderr=(
                f"loading model from '{self.model}'\n"
                "whisper_backend_init_gpu: using Vulkan0 backend\n"
            ).encode(),
            stdout=b"GEHEIMES TRANSKRIPT\n",
        )
        engine, popen, _ = self.engine(process=process)

        engine.start()

        self.assertTrue(engine.ready)
        self.assertTrue(engine.backend_verified)
        self.assertEqual(engine.backend, Backend.VULKAN)
        self.assertEqual(engine.base_url, "http://127.0.0.1:45123/private-token")
        args, kwargs = popen.calls[0]
        self.assertEqual(args[0], "whisper-server")
        expected_pairs = {
            "--model": str(self.model),
            "--vad-model": str(self.vad),
            "--host": "127.0.0.1",
            "--port": "45123",
            "--request-path": "/private-token",
            "--tmp-dir": str(self.runtime),
            "--language": "de",
            "--threads": "8",
        }
        for flag, value in expected_pairs.items():
            self.assertIn(flag, args)
            self.assertEqual(args[args.index(flag) + 1], value)
        for flag in ("--vad", "--no-timestamps", "--flash-attn"):
            self.assertIn(flag, args)
        self.assertNotIn("--no-context", args)
        self.assertNotIn("--convert", args)
        self.assertNotIn("--no-gpu", args)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        diagnostics = "\n".join(engine.diagnostics)
        self.assertNotIn("GEHEIMES TRANSKRIPT", diagnostics)
        self.assertNotIn(str(self.model), diagnostics)
        self.assertIn("<private-path>", diagnostics)
        engine.stop()

    def test_cpu_is_only_selected_explicitly_and_adds_no_gpu(self):
        config = self.config(backend="cpu")
        process = FakeProcess(stderr=b"whisper_backend_init_gpu: using CPU backend\n")
        engine, popen, _ = self.engine(config=config, process=process)

        engine.start()

        self.assertTrue(engine.ready)
        self.assertTrue(engine.backend_verified)
        self.assertIn("--no-gpu", popen.calls[0][0])
        engine.stop()

    def test_vulkan_health_without_backend_evidence_is_rejected_and_stopped(self):
        process = FakeProcess(stderr=b"whisper_backend_init_gpu: using CPU backend\n")
        engine, _, _ = self.engine(process=process)

        with self.assertRaises(BackendUnavailableError):
            engine.start()

        self.assertFalse(engine.ready)
        self.assertIn(signal.SIGINT, process.signals)
        self.assertIsNone(engine.base_url)

    def test_transcription_uses_exact_fixed_fields_and_returns_raw_text(self):
        response = FakeResponse(payload={"text": "  Grüß Gott.\n"})
        session = FakeSession(transcription=response)
        engine, _, _ = self.engine(session=session)
        engine.start()

        result = engine.transcribe(
            self.wav,
            initial_prompt="CachyOS, KDE Plasma",
            timeout=7.5,
        )

        self.assertEqual(result, "  Grüß Gott.\n")
        self.assertEqual(len(session.post_calls), 1)
        call = session.post_calls[0]
        self.assertEqual(call["url"], f"{engine.base_url}/inference")
        self.assertEqual(
            call["data"],
            {
                "language": "de",
                "translate": "false",
                "detect_language": "false",
                "prompt": "CachyOS, KDE Plasma",
                "carry_initial_prompt": "true",
                "vad": "true",
                "no_timestamps": "true",
                "temperature": "0.0",
                "temperature_inc": "0.2",
                "response_format": "json",
            },
        )
        self.assertEqual(call["filename"], "speech.wav")
        self.assertEqual(call["audio"], b"RIFFtest")
        self.assertEqual(call["mime"], "audio/wav")
        self.assertEqual(call["timeout"], (0.25, 7.5))
        self.assertTrue(response.closed)
        engine.stop()

    def test_start_can_warm_up_vad_and_main_model(self):
        session = FakeSession(transcription=FakeResponse(payload={"text": "warm"}))
        engine, _, _ = self.engine(session=session)

        engine.start(warmup_wav=self.wav)

        self.assertEqual(len(session.post_calls), 1)
        self.assertEqual(session.post_calls[0]["data"]["vad"], "true")
        self.assertEqual(session.post_calls[0]["data"]["prompt"], "")
        engine.stop()

    def test_port_bind_race_retries_with_new_port_and_path(self):
        collision = FakeProcess(
            stderr=b"couldn't bind to server socket: hostname=127.0.0.1 port=41001\n",
            returncode=1,
        )
        healthy = FakeProcess(stderr=VULKAN_LOG)
        popen = FakePopen(collision, healthy)
        paths = iter(["/first-secret", "/second-secret"])
        ports = iter([41001, 41002])
        engine = WhisperEngine(
            self.config(),
            popen_factory=popen,
            session=FakeSession(),
            port_chooser=lambda: next(ports),
            port_available=lambda _port: True,
            request_path_factory=lambda: next(paths),
        )

        engine.start()

        self.assertEqual(len(popen.calls), 2)
        self.assertEqual(engine.port, 41002)
        self.assertEqual(engine.request_path, "/second-secret")
        engine.stop()

    def test_preflight_skips_a_port_that_is_already_busy(self):
        process = FakeProcess(stderr=VULKAN_LOG)
        popen = FakePopen(process)
        ports = iter([42001, 42002])
        engine = WhisperEngine(
            self.config(),
            popen_factory=popen,
            session=FakeSession(),
            port_chooser=lambda: next(ports),
            port_available=lambda port: port == 42002,
            request_path_factory=lambda: "/secret",
        )

        engine.start()

        self.assertEqual(len(popen.calls), 1)
        self.assertEqual(engine.port, 42002)
        engine.stop()

    def test_invalid_or_error_responses_never_echo_server_body(self):
        cases = [
            FakeResponse(status_code=500, payload={"text": "private transcript"}),
            FakeResponse(payload={"unexpected": "private transcript"}),
            FakeResponse(json_error=ValueError("private transcript")),
        ]
        for response in cases:
            with self.subTest(response=response):
                session = FakeSession(transcription=response)
                engine, _, _ = self.engine(session=session)
                engine.start()
                with self.assertRaises(TranscriptionError) as caught:
                    engine.transcribe(self.wav)
                self.assertNotIn("private transcript", str(caught.exception))
                engine.stop()

    def test_request_timeout_and_external_cancellation_are_typed(self):
        timeout_session = FakeSession(transcription=requests.ReadTimeout("secret body"))
        engine, _, _ = self.engine(session=timeout_session)
        engine.start()
        with self.assertRaises(TranscriptionTimeout) as caught:
            engine.transcribe(self.wav)
        self.assertNotIn("secret body", str(caught.exception))
        engine.stop()

        cancel = threading.Event()
        cancel.set()
        engine, _, _ = self.engine()
        engine.start()
        with self.assertRaises(EngineCancelled):
            engine.transcribe(self.wav, cancel_event=cancel)
        engine.stop()

    def test_unexpected_process_exit_is_reported(self):
        process = FakeProcess(stderr=b"model init failed\n", returncode=23)
        engine, _, _ = self.engine(process=process)
        with self.assertRaises(EngineProcessError) as caught:
            engine.start()
        self.assertIn("23", str(caught.exception))
        self.assertFalse(engine.ready)

    def test_stop_escalates_sigint_terminate_kill(self):
        process = FakeProcess(stderr=VULKAN_LOG, stubborn=True)
        engine, _, _ = self.engine(process=process)
        engine.start()

        engine.stop()

        self.assertEqual(process.signals, [signal.SIGINT])
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertFalse(engine.ready)
        self.assertIsNone(engine.port)

    def test_missing_models_and_invalid_private_path_fail_before_readiness(self):
        self.model.unlink()
        engine, popen, _ = self.engine()
        with self.assertRaises(EngineConfigurationError):
            engine.start()
        self.assertEqual(popen.calls, [])

        self.model.write_bytes(b"model")
        engine, popen, _ = self.engine(path="/nested/path")
        with self.assertRaises(EngineConfigurationError):
            engine.start()
        self.assertEqual(popen.calls, [])

    def test_health_requires_status_ok_json(self):
        sessions = [
            FakeSession(health=[FakeResponse(payload={"status": "loading model"})]),
            FakeSession(health=[FakeResponse(status_code=503)]),
            FakeSession(health=[FakeResponse(json_error=ValueError("bad"))]),
        ]
        for session in sessions:
            with self.subTest(session=session):
                engine, _, _ = self.engine(session=session)
                with self.assertRaises(EngineStartError):
                    engine.start()


if __name__ == "__main__":
    unittest.main()
