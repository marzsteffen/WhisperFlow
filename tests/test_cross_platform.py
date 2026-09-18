from __future__ import annotations

import hashlib
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import install
from local_dictation.microphones import _list_windows_microphones
from local_dictation.windows_insertion import WindowsInsertionBackend
from local_dictation.windows_recorder import WindowsRecorder


def test_windows_microphones_use_stable_device_indices() -> None:
    sounddevice = SimpleNamespace(
        query_devices=lambda: [
            {"name": "Speakers", "max_input_channels": 0},
            {"name": "USB microphone", "max_input_channels": 2},
        ]
    )

    microphones = _list_windows_microphones(sounddevice)

    assert [(item.node_name, item.description, item.object_id) for item in microphones] == [
        ("1", "USB microphone", 1)
    ]


def test_windows_recorder_creates_whisper_ready_wav(monkeypatch, tmp_path: Path) -> None:
    streams = []

    class FakeStream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.started = False
            streams.append(self)

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=FakeStream))
    recorder = WindowsRecorder(tmp_path)
    finished = []
    recorder.finished.connect(lambda path, duration, limited: finished.append((path, duration, limited)))

    recorder.start("1", 1)
    streams[0].callback(b"\x10\x00" * 8_000, 8_000, None, False)
    recorder.stop()

    assert len(finished) == 1
    path = Path(finished[0][0])
    with wave.open(str(path), "rb") as recording:
        assert recording.getnchannels() == 1
        assert recording.getframerate() == 16_000
        assert recording.getsampwidth() == 2
        assert recording.getnframes() == 8_000
    recorder.shutdown()


def test_windows_insertion_copies_then_sends_paste(monkeypatch, tmp_path: Path) -> None:
    copied = []
    sent = []
    monkeypatch.setitem(sys.modules, "pyperclip", SimpleNamespace(copy=copied.append))
    monkeypatch.setitem(sys.modules, "keyboard", SimpleNamespace(send=sent.append))
    backend = WindowsInsertionBackend(tmp_path)

    result = backend.insert("Hallo Welt")

    assert result.copied and result.inserted
    assert copied == ["Hallo Welt"]
    assert sent == ["shift+insert"]


def test_installer_download_verifies_and_atomically_names_file(tmp_path: Path) -> None:
    payload = b"verified installer payload"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    destination = tmp_path / "downloads" / "target.bin"

    result = install.download(
        source.as_uri(),
        destination,
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        "Testdatei",
        10,
        20,
    )

    assert result == destination
    assert destination.read_bytes() == payload
    assert not destination.with_name("target.bin.part").exists()


def test_installer_writes_selected_model_and_cpu_backend(tmp_path: Path) -> None:
    targets = {
        "config": tmp_path / "config",
        "models": tmp_path / "models",
    }

    install.write_config(targets, "ggml-base.bin")

    config = json.loads((targets["config"] / "config.json").read_text(encoding="utf-8"))
    assert config["model_path"] == str(targets["models"] / "ggml-base.bin")
    assert config["backend"] == "cpu"
    assert config["trigger"] == "KEY_RIGHTCTRL"


def test_installer_ui_and_quickstart_are_shared_assets() -> None:
    assert "Small · empfohlen" not in install.HTML  # HTML uses compact line breaks.
    assert 'value="small" checked' in install.HTML
    assert "/quickstart.svg" in install.HTML
    assert (install.ROOT / "assets" / "quickstart.svg").is_file()
