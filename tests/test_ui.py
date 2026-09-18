from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from local_dictation.config import (
    DiagnosticsConfig,
    LiveConfig,
    RecordingConfig,
    default_config,
)
from local_dictation.microphones import Microphone
from local_dictation.ui import DownloadDialog, Overlay, SettingsDialog, Tray


@pytest.fixture(scope="module", autouse=True)
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication(["local-dictation-test"])
    assert isinstance(app, QApplication)
    return app


def test_release_timeout_overlay_does_not_hide_later_recording_message() -> None:
    overlay = Overlay()
    assert overlay.label.wordWrap()
    assert overlay.label.maximumWidth() == 650
    overlay.show_message("Fertig", kind="success", timeout_ms=1000)
    assert overlay._hide_timer.isActive()
    overlay.show_message("Aufnahme …", kind="recording")
    assert not overlay._hide_timer.isActive()
    assert overlay.label.text() == "Aufnahme …"
    assert not overlay.progress.isVisible()
    overlay.close()


def test_settings_save_round_trips_entire_valid_recording_range() -> None:
    original = default_config()
    original = replace(
        original,
        recording=RecordingConfig(
            min_duration_ms=1,
            max_duration_s=1,
            silence_threshold_dbfs=-150.0,
        ),
    )
    dialog = SettingsDialog(original)
    dialog.set_microphones(
        [Microphone(original.microphone_id, "Test microphone", False)],
        original.microphone_id,
    )
    saved: list[object] = []
    dialog.saved.connect(saved.append)

    dialog._save()

    assert saved == [original]
    dialog.close()


def test_live_preview_controls_are_bounded_explained_and_saved() -> None:
    config = default_config()
    dialog = SettingsDialog(config)
    dialog.set_microphones(
        [Microphone(config.microphone_id, "Test microphone", False)],
        config.microphone_id,
    )
    saved: list[object] = []
    dialog.saved.connect(saved.append)

    assert not dialog.live_enabled.isChecked()
    assert not dialog.live_direct_insert.isChecked()
    assert not dialog.live_direct_insert.isEnabled()
    assert not dialog.live_interval.isEnabled()
    assert dialog.live_interval.minimum() == 1000
    assert dialog.live_interval.maximum() == 5000
    assert dialog.live_interval.singleStep() == 250
    assert dialog.live_interval.value() == 1500
    assert "laufenden Entwurf noch korrigieren" in dialog.live_notice.text()
    assert "Fokus und Cursor" in dialog.live_notice.text()
    assert "bis zur Abschlussmeldung" in dialog.live_notice.text()
    assert "nur kopiert" in dialog.live_notice.text()

    dialog.live_enabled.setChecked(True)
    dialog.live_direct_insert.setChecked(True)
    dialog.live_interval.setValue(2750)
    dialog._save()

    assert dialog.live_interval.isEnabled()
    assert dialog.live_direct_insert.isEnabled()
    assert len(saved) == 1
    assert saved[0].live == LiveConfig(
        enabled=True,
        direct_insert=True,
        interval_ms=2750,
    )
    dialog.close()


def test_existing_live_settings_round_trip() -> None:
    config = replace(
        default_config(),
        live=LiveConfig(enabled=True, direct_insert=True, interval_ms=5000),
    )
    dialog = SettingsDialog(config)
    dialog.set_microphones(
        [Microphone(config.microphone_id, "Test microphone", False)],
        config.microphone_id,
    )
    saved: list[object] = []
    dialog.saved.connect(saved.append)

    assert dialog.live_enabled.isChecked()
    assert dialog.live_direct_insert.isChecked()
    assert dialog.live_interval.isEnabled()
    assert dialog.live_interval.value() == 5000
    dialog._save()

    assert saved == [config]
    dialog.close()


def test_diagnostics_are_opt_in_with_private_path_and_bounded_retention() -> None:
    dialog = SettingsDialog(default_config())

    assert not dialog.diagnostics_enabled.isChecked()
    assert not dialog.diagnostics_retention.isEnabled()
    assert dialog.diagnostics_retention.minimum() == 1
    assert dialog.diagnostics_retention.maximum() == 100
    assert dialog.diagnostics_retention.value() == 20
    assert dialog.diagnostics_path.text().endswith("/local-dictation/diagnostics")
    notice = dialog.diagnostics_notice.text()
    assert "Audio-WAV" in notice
    assert "Transkript" in notice
    assert "Fehler" in notice
    assert "dauerhaft" in notice
    assert "vollständig gelöscht" in notice
    dialog.close()


def test_first_diagnostics_activation_requires_confirmation_and_saves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    questions: list[tuple[object, ...]] = []

    def accept(*args: object) -> QMessageBox.StandardButton:
        questions.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", accept)
    config = default_config()
    dialog = SettingsDialog(config)
    dialog.set_microphones(
        [Microphone(config.microphone_id, "Test microphone", 1)],
        config.microphone_id,
    )
    saved: list[object] = []
    dialog.saved.connect(saved.append)

    dialog.diagnostics_enabled.setChecked(True)
    dialog.diagnostics_enabled.setChecked(False)
    dialog.diagnostics_enabled.setChecked(True)
    dialog.diagnostics_retention.setValue(100)
    dialog._save()

    assert len(questions) == 1
    assert "Audio-WAV" in str(questions[0][2])
    assert "vertrauliche Inhalte" in str(questions[0][2])
    assert dialog.diagnostics_retention.isEnabled()
    assert len(saved) == 1
    assert saved[0].diagnostics == DiagnosticsConfig(
        enabled=True,
        retention_entries=100,
    )
    dialog.close()


def test_declined_diagnostics_activation_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )
    dialog = SettingsDialog(default_config())

    dialog.diagnostics_enabled.setChecked(True)

    assert not dialog.diagnostics_enabled.isChecked()
    assert not dialog.diagnostics_retention.isEnabled()
    dialog.close()


def test_existing_diagnostics_opt_in_round_trips_without_new_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(
        default_config(),
        diagnostics=DiagnosticsConfig(enabled=True, retention_entries=37),
    )
    dialog = SettingsDialog(config)
    dialog.set_microphones(
        [Microphone(config.microphone_id, "Test microphone", 1)],
        config.microphone_id,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: pytest.fail("existing opt-in must not ask again"),
    )
    saved: list[object] = []
    dialog.saved.connect(saved.append)

    dialog._save()

    assert saved == [config]
    dialog.close()


def test_diagnostics_open_and_confirmed_clear_emit_separate_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(
        [
            QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ]
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: next(answers))
    dialog = SettingsDialog(default_config())
    opened: list[bool] = []
    cleared: list[bool] = []
    dialog.diagnostics_open_requested.connect(lambda: opened.append(True))
    dialog.diagnostics_clear_requested.connect(lambda: cleared.append(True))

    dialog.diagnostics_open_button.click()
    dialog.diagnostics_clear_button.click()
    dialog.diagnostics_clear_button.click()

    assert opened == [True]
    assert cleared == [True]
    dialog.close()


def test_download_close_requests_cancellation_exactly_once() -> None:
    dialog = DownloadDialog()
    cancellations: list[bool] = []
    dialog.cancelled.connect(lambda: cancellations.append(True))
    dialog.reject()
    dialog.reject()
    assert cancellations == [True]


def test_tray_actions_emit_public_signals_and_mic_identity() -> None:
    tray = Tray()
    settings: list[bool] = []
    selected: list[str] = []
    tray.show_settings.connect(lambda: settings.append(True))
    tray.microphone_selected.connect(selected.append)
    microphones = [
        Microphone("node.one", "Intern", 1, muted=False),
        Microphone("node.two", "USB", 2, muted=True),
    ]
    tray.set_microphones(microphones, "node.one")

    next(
        action
        for action in tray.menu.actions()
        if action.text() == "Fachwortliste und Einstellungen …"
    ).trigger()
    tray.microphone_menu.actions()[1].trigger()

    assert settings == [True]
    assert selected == ["node.two"]
    assert tray.microphone_menu.actions()[1].text() == "USB (stumm)"
