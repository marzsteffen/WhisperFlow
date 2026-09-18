from pathlib import Path

from local_dictation import cli


def test_purge_removes_only_managed_xdg_directories(tmp_path: Path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    config = config_home / "local-dictation"
    data = data_home / "local-dictation"
    config.mkdir(parents=True)
    data.mkdir(parents=True)
    (config / "config.json").write_text("{}")
    (data / "model.bin").write_bytes(b"model")
    keep = data_home / "keep.txt"
    keep.write_text("keep")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: None)

    assert cli.purge(assume_yes=True) == 0
    assert not config.exists()
    assert not data.exists()
    assert keep.read_text() == "keep"


def test_purge_rejects_symlink(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "local-dictation").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    try:
        cli.purge(assume_yes=True)
    except RuntimeError as exc:
        assert "Unsicherer Purge-Pfad" in str(exc)
    else:
        raise AssertionError("symlink purge target was accepted")

