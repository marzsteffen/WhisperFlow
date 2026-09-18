from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import AppConfig, DiagnosticsConfig, LiveConfig, RecordingConfig, get_data_dir
from .microphones import Microphone
from .model_store import WHISPER_MODELS

APP_STYLESHEET = """
QWidget { color: #e8edf6; font-family: "Segoe UI", "Inter", sans-serif; font-size: 14px; }
QDialog, QMenu { background: #10131a; }
QGroupBox { border: 1px solid #2a3140; border-radius: 14px; margin-top: 14px; padding: 14px; font-weight: 650; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 7px; color: #9fb6ff; }
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
  background: #191e28; border: 1px solid #343d50; border-radius: 9px; padding: 8px; selection-background-color: #6d7cff;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #7c8cff; }
QPushButton { background: #252c3a; border: 1px solid #3b465c; border-radius: 9px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #30394b; border-color: #7183ff; }
QPushButton:pressed { background: #1e2430; }
QPushButton:default { background: #6577f3; border-color: #8795ff; color: white; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QProgressBar { background: #1b202a; border: 0; border-radius: 6px; height: 12px; text-align: center; }
QProgressBar::chunk { background: #7183ff; border-radius: 6px; }
QMenu { border: 1px solid #31394a; padding: 6px; }
QMenu::item { border-radius: 7px; padding: 7px 24px; }
QMenu::item:selected { background: #2b3447; }
"""


def apply_theme(app: QApplication) -> None:
    """Apply the exact same modern visual system on Linux and Windows."""

    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)


def status_icon(color: str = "#3daee9") -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(12, 7, 40, 40)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(27, 15, 10, 23, 5, 5)
    painter.drawRoundedRect(21, 35, 22, 5, 2, 2)
    painter.drawRoundedRect(29, 39, 6, 12, 2, 2)
    painter.end()
    return QIcon(pixmap)


class Overlay(QWidget):
    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(10)
        self.dot = QLabel("●")
        self.dot.setStyleSheet("font-size: 22px; color: #da4453")
        self.label = QLabel("")
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(650)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(90)
        self.progress.hide()
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        self.setStyleSheet(
            "QWidget { background: rgba(30, 30, 30, 225); color: white; "
            "border-radius: 12px; font-size: 14px; }"
        )
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def show_message(self, message: str, *, kind: str, timeout_ms: int = 0) -> None:
        colors = {
            "recording": "#da4453",
            "working": "#fdbc4b",
            "success": "#27ae60",
            "error": "#ed1515",
            "neutral": "#3daee9",
        }
        self.dot.setStyleSheet(f"font-size: 22px; color: {colors.get(kind, colors['neutral'])}")
        self.label.setText(message)
        self.progress.setVisible(kind == "working")
        self.adjustSize()
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2, area.bottom() - self.height() - 36)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        if timeout_ms:
            self._hide_timer.start(timeout_ms)
        else:
            self._hide_timer.stop()


class DownloadDialog(QDialog):
    cancelled = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("WhisperFlow – Modelle")
        self.setModal(False)
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        self.label = QLabel("Modelle werden vorbereitet …")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.details = QLabel("")
        self.cancel_button = QPushButton("Abbrechen")
        self.cancel_button.clicked.connect(self._request_cancel)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addWidget(self.details)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignRight)

    def _request_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancelled.emit()

    def reject(self) -> None:
        # Closing the window must cancel the network/engine operation as well.
        if self.cancel_button.isEnabled():
            self._request_cancel()
        super().reject()

    def update_progress(self, name: str, downloaded: int, total: int) -> None:
        self.label.setText(f"{name} wird heruntergeladen …")
        self.progress.setValue(int(downloaded * 1000 / total) if total else 0)
        self.details.setText(f"{downloaded / 1024**2:.1f} von {total / 1024**2:.1f} MiB")


class SettingsDialog(QDialog):
    saved = pyqtSignal(object)
    download_requested = pyqtSignal()
    diagnostics_open_requested = pyqtSignal()
    diagnostics_clear_requested = pyqtSignal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self.setWindowTitle("WhisperFlow – Einstellungen")
        self.setMinimumSize(620, 620)
        outer = QVBoxLayout(self)
        form = QFormLayout()

        self.enabled = QCheckBox("Rechte Strg-Taste für Diktat reservieren")
        self.enabled.setChecked(config.enabled)
        self.microphone = QComboBox()
        self.model_choice = QComboBox()
        model_labels = {
            "tiny": "Tiny · sehr schnell · 75 MB",
            "base": "Base · schnell · 142 MB",
            "small": "Small · empfohlen · 465 MB",
            "medium": "Medium · genauer · 1,5 GB",
            "large-v3-turbo": "Large v3 Turbo · höchste Qualität · 1,6 GB",
        }
        for key, spec in WHISPER_MODELS.items():
            self.model_choice.addItem(model_labels[key], spec.filename)
        self.model_choice.addItem("Eigene GGML-Datei", "custom")
        self.backend = QComboBox()
        self.backend.addItem("Vulkan (Radeon 860M)", "vulkan")
        self.backend.addItem("CPU (expliziter Fallback)", "cpu")
        self.backend.setCurrentIndex(max(0, self.backend.findData(config.backend)))
        self.prompt = QTextEdit(config.initial_prompt)
        self.prompt.setAcceptRichText(False)
        self.prompt.setMaximumHeight(130)
        self.model_path = QLineEdit(config.model_path)
        selected_model = next(
            (spec.filename for spec in WHISPER_MODELS.values() if spec.filename == Path(config.model_path).name),
            "custom",
        )
        self.model_choice.setCurrentIndex(max(0, self.model_choice.findData(selected_model)))
        self.model_choice.currentIndexChanged.connect(self._model_selected)
        self.vad_model_path = QLineEdit(config.vad_model_path)
        self.min_duration = QSpinBox()
        self.min_duration.setRange(1, 300_000)
        self.min_duration.setSuffix(" ms")
        self.min_duration.setValue(config.recording.min_duration_ms)
        self.max_duration = QSpinBox()
        self.max_duration.setRange(1, 300)
        self.max_duration.setSuffix(" s")
        self.max_duration.setValue(config.recording.max_duration_s)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(-200.0, 0.0)
        self.threshold.setSuffix(" dBFS")
        self.threshold.setValue(config.recording.silence_threshold_dbfs)
        self.model_status = QLabel("Noch nicht geprüft")
        self.live_enabled = QCheckBox(
            "Erkannten Text während der Aufnahme als Live-Vorschau anzeigen"
        )
        self.live_enabled.setChecked(config.live.enabled)
        self.live_direct_insert = QCheckBox(
            "Experimentell: Live-Text direkt ins aktive Feld einfügen und revidieren"
        )
        self.live_direct_insert.setChecked(config.live.direct_insert)
        self.live_direct_insert.setEnabled(config.live.enabled)
        self.live_interval = QSpinBox()
        self.live_interval.setRange(1000, 5000)
        self.live_interval.setSingleStep(250)
        self.live_interval.setSuffix(" ms")
        self.live_interval.setValue(config.live.interval_ms)
        self.live_interval.setEnabled(config.live.enabled)
        self.live_enabled.toggled.connect(self.live_interval.setEnabled)
        self.live_enabled.toggled.connect(self.live_direct_insert.setEnabled)
        self.live_notice = QLabel(
            "Whisper kann den laufenden Entwurf noch korrigieren. Im experimentellen "
            "Direktmodus ersetzt die App deshalb ihren bereits eingefügten Text. "
            "Fokus und Cursor bitte bis zur Abschlussmeldung nicht wechseln. Bei einer "
            "erkannten Störung wird die Endfassung nur kopiert."
        )
        self.live_notice.setWordWrap(True)
        self.live_notice.setStyleSheet("color: #c45b00; font-weight: 600")
        self._diagnostics_path = get_data_dir() / "diagnostics"
        self._diagnostics_opt_in_confirmed = config.diagnostics.enabled

        self.diagnostics_enabled = QCheckBox(
            "Diagnosemodus aktivieren und Sprachdaten dauerhaft speichern"
        )
        self.diagnostics_enabled.setChecked(config.diagnostics.enabled)
        self.diagnostics_enabled.toggled.connect(self._diagnostics_toggled)
        self.diagnostics_retention = QSpinBox()
        self.diagnostics_retention.setRange(1, 100)
        self.diagnostics_retention.setSuffix(" Fälle")
        self.diagnostics_retention.setValue(config.diagnostics.retention_entries)
        self.diagnostics_retention.setEnabled(config.diagnostics.enabled)
        self.diagnostics_notice = QLabel(
            "Datenschutzhinweis: Bei aktiviertem Diagnosemodus werden Audio-WAV, "
            "Transkript und Fehler dauerhaft lokal gespeichert. Beim Deaktivieren "
            "bleiben vorhandene Diagnosedaten erhalten, bis sie vollständig gelöscht werden."
        )
        self.diagnostics_notice.setWordWrap(True)
        self.diagnostics_notice.setStyleSheet("color: #c45b00; font-weight: 600")
        self.diagnostics_path = QLabel(str(self._diagnostics_path))
        self.diagnostics_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.diagnostics_path.setWordWrap(True)
        self.diagnostics_open_button = QPushButton("Diagnoseordner öffnen")
        self.diagnostics_open_button.clicked.connect(self.diagnostics_open_requested.emit)
        self.diagnostics_clear_button = QPushButton(
            "Alle Diagnosedaten vollständig löschen …"
        )
        self.diagnostics_clear_button.clicked.connect(self._request_diagnostics_clear)

        form.addRow("Bereitschaft", self.enabled)
        form.addRow("Mikrofon", self.microphone)
        form.addRow("Backend", self.backend)
        form.addRow("Sprachmodell", self.model_choice)
        form.addRow("Fachwörter / Initial-Prompt", self.prompt)
        form.addRow("Whisper-Modell", self._path_row(self.model_path))
        form.addRow("VAD-Modell", self._path_row(self.vad_model_path))
        form.addRow("Mindestaufnahme", self.min_duration)
        form.addRow("Maximalaufnahme", self.max_duration)
        form.addRow("Stillegrenze", self.threshold)
        form.addRow("Modellstatus", self.model_status)
        outer.addLayout(form)

        live_box = QGroupBox("Live-Transkription")
        live_layout = QVBoxLayout(live_box)
        live_layout.addWidget(self.live_enabled)
        live_layout.addWidget(self.live_direct_insert)
        live_layout.addWidget(self.live_notice)
        live_form = QFormLayout()
        live_form.addRow("Aktualisierungsintervall", self.live_interval)
        live_layout.addLayout(live_form)
        outer.addWidget(live_box)

        diagnostics_box = QGroupBox("Lokale Diagnose")
        diagnostics_layout = QVBoxLayout(diagnostics_box)
        diagnostics_layout.addWidget(self.diagnostics_enabled)
        diagnostics_layout.addWidget(self.diagnostics_notice)
        diagnostics_form = QFormLayout()
        diagnostics_form.addRow("Aufbewahrung", self.diagnostics_retention)
        diagnostics_form.addRow("Privater Speicherort", self.diagnostics_path)
        diagnostics_layout.addLayout(diagnostics_form)
        diagnostics_buttons = QHBoxLayout()
        diagnostics_buttons.addWidget(self.diagnostics_open_button)
        diagnostics_buttons.addWidget(self.diagnostics_clear_button)
        diagnostics_buttons.addStretch()
        diagnostics_layout.addLayout(diagnostics_buttons)
        outer.addWidget(diagnostics_box)

        button_row = QHBoxLayout()
        download = QPushButton("Offizielle Modelle laden / reparieren")
        download.clicked.connect(self.download_requested)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Speichern")
        save.setDefault(True)
        save.clicked.connect(self._save)
        button_row.addWidget(download)
        button_row.addStretch()
        button_row.addWidget(cancel)
        button_row.addWidget(save)
        outer.addLayout(button_row)

    def _path_row(self, edit: QLineEdit) -> QWidget:
        widget = QWidget()
        row = QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("…")
        browse.setFixedWidth(36)
        browse.clicked.connect(lambda: self._browse(edit))
        row.addWidget(edit)
        row.addWidget(browse)
        return widget

    def _browse(self, edit: QLineEdit) -> None:
        selected, _ = QFileDialog.getOpenFileName(self, "GGML-Modell wählen", edit.text(), "GGML (*.bin)")
        if selected:
            edit.setText(selected)
            if edit is self.model_path:
                self.model_choice.setCurrentIndex(self.model_choice.findData("custom"))

    def _model_selected(self) -> None:
        filename = self.model_choice.currentData()
        if filename and filename != "custom":
            self.model_path.setText(str(get_data_dir() / "models" / str(filename)))

    def set_microphones(self, microphones: list[Microphone], selected: str) -> None:
        self.microphone.clear()
        for mic in microphones:
            suffix = " (stumm)" if mic.muted else ""
            self.microphone.addItem(f"{mic.description}{suffix}", mic.node_name)
        index = self.microphone.findData(selected)
        if index < 0 and selected:
            self.microphone.addItem(f"Nicht verfügbar: {selected}", selected)
            index = self.microphone.count() - 1
        self.microphone.setCurrentIndex(max(0, index))

    def set_model_status(self, text: str) -> None:
        self.model_status.setText(text)

    def update_config(self, config: AppConfig) -> None:
        self._config = config

    def _diagnostics_toggled(self, enabled: bool) -> None:
        if enabled and not self._diagnostics_opt_in_confirmed:
            if not self._confirm_diagnostics_opt_in():
                self.diagnostics_enabled.blockSignals(True)
                self.diagnostics_enabled.setChecked(False)
                self.diagnostics_enabled.blockSignals(False)
                self.diagnostics_retention.setEnabled(False)
                return
            self._diagnostics_opt_in_confirmed = True
        self.diagnostics_retention.setEnabled(enabled)

    def _confirm_diagnostics_opt_in(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Diagnosemodus aktivieren?",
            "Der Diagnosemodus speichert Ihre Sprachaufnahme als Audio-WAV sowie "
            "Transkript und Fehler dauerhaft auf diesem Gerät. Die Daten können "
            "vertrauliche Inhalte enthalten.\n\n"
            f"Privater Speicherort:\n{self._diagnostics_path}\n\n"
            "Möchten Sie den Diagnosemodus ausdrücklich aktivieren?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _request_diagnostics_clear(self) -> None:
        answer = QMessageBox.question(
            self,
            "Diagnosedaten vollständig löschen?",
            "Alle gespeicherten Audio-WAVs, Transkripte und Fehlerberichte im "
            "Diagnoseordner werden unwiderruflich gelöscht. Modelle und Einstellungen "
            "bleiben erhalten.\n\nFortfahren?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.diagnostics_clear_requested.emit()

    def _save(self) -> None:
        prompt = self.prompt.toPlainText()
        if len(prompt) > 2048:
            QMessageBox.warning(self, "Prompt zu lang", "Der Initial-Prompt darf höchstens 2048 Zeichen enthalten.")
            return
        microphone_id = self.microphone.currentData()
        if not microphone_id:
            QMessageBox.warning(self, "Kein Mikrofon", "Bitte ein Mikrofon auswählen.")
            return
        if self.max_duration.value() * 1000 < self.min_duration.value():
            QMessageBox.warning(
                self,
                "Ungültige Aufnahmegrenzen",
                "Die Maximaldauer muss mindestens die Mindestaufnahme abdecken.",
            )
            return
        recording = RecordingConfig(
            min_duration_ms=self.min_duration.value(),
            max_duration_s=self.max_duration.value(),
            silence_threshold_dbfs=self.threshold.value(),
        )
        diagnostics = DiagnosticsConfig(
            enabled=self.diagnostics_enabled.isChecked(),
            retention_entries=self.diagnostics_retention.value(),
        )
        live = LiveConfig(
            enabled=self.live_enabled.isChecked(),
            direct_insert=self.live_direct_insert.isChecked(),
            interval_ms=self.live_interval.value(),
        )
        config = replace(
            self._config,
            enabled=self.enabled.isChecked(),
            microphone_id=str(microphone_id),
            backend=str(self.backend.currentData()),
            initial_prompt=prompt,
            model_path=self.model_path.text().strip(),
            vad_model_path=self.vad_model_path.text().strip(),
            recording=recording,
            live=live,
            diagnostics=diagnostics,
        )
        self.saved.emit(config)
        self.accept()


class Tray(QObject):
    show_settings = pyqtSignal()
    enabled_changed = pyqtSignal(bool)
    microphone_selected = pyqtSignal(str)
    copy_last = pyqtSignal()
    benchmark = pyqtSignal()
    restart_engine = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.icon = QSystemTrayIcon(status_icon(), self)
        self.icon.setToolTip("WhisperFlow")
        self.menu = QMenu()
        self.ready_action = self.menu.addAction("Status: wird gestartet …")
        self.ready_action.setEnabled(False)
        self.model_action = self.menu.addAction("Modell: wird geprüft …")
        self.model_action.setEnabled(False)
        self.menu.addSeparator()
        self.enabled_action = self.menu.addAction("Diktat aktiv")
        self.enabled_action.setCheckable(True)
        self.enabled_action.toggled.connect(self.enabled_changed)
        self.microphone_menu = self.menu.addMenu("Mikrofon")
        self._microphone_group = QActionGroup(self)
        self._microphone_group.setExclusive(True)
        settings_action = self.menu.addAction("Fachwortliste und Einstellungen …")
        settings_action.triggered.connect(self.show_settings.emit)
        self.copy_action = self.menu.addAction("Letztes Ergebnis kopieren")
        self.copy_action.triggered.connect(self.copy_last.emit)
        self.copy_action.setEnabled(False)
        benchmark_action = self.menu.addAction("Benchmark …")
        benchmark_action.triggered.connect(self.benchmark.emit)
        restart_action = self.menu.addAction("Engine neu starten")
        restart_action.triggered.connect(self.restart_engine.emit)
        self.menu.addSeparator()
        quit_action = self.menu.addAction("Beenden")
        quit_action.triggered.connect(self.quit_requested.emit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)

    def show(self) -> None:
        self.icon.show()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_settings.emit()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled_action.blockSignals(True)
        self.enabled_action.setChecked(enabled)
        self.enabled_action.blockSignals(False)

    def set_status(self, ready: str, model: str, *, color: str = "#3daee9") -> None:
        self.ready_action.setText(f"Status: {ready}")
        self.model_action.setText(f"Modell: {model}")
        self.icon.setIcon(status_icon(color))
        self.icon.setToolTip(f"WhisperFlow – {ready}")

    def set_last_available(self, available: bool) -> None:
        self.copy_action.setEnabled(available)

    def set_microphones(self, microphones: list[Microphone], selected: str) -> None:
        self.microphone_menu.clear()
        self._microphone_group = QActionGroup(self)
        self._microphone_group.setExclusive(True)
        if not microphones:
            action = self.microphone_menu.addAction("Keine Quelle gefunden")
            action.setEnabled(False)
            return
        for mic in microphones:
            label = mic.description + (" (stumm)" if mic.muted else "")
            action = self.microphone_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(mic.node_name == selected)
            action.setData(mic.node_name)
            self._microphone_group.addAction(action)
            action.triggered.connect(
                lambda checked=False, node=mic.node_name: self.microphone_selected.emit(node)
            )
