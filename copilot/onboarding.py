"""The two windows a non-technical user actually sees.

Nod was configured by environment variables and a 100-character command line.
That is fine for the person who wrote it and impossible for anyone else: setting
GEMINI_API_KEY on Windows means `setx` and a *new* terminal, and there is no
terminal in a packaged build.

A generated text file was the obvious cheap alternative and it is the wrong
answer for this audience. "Open %USERPROFILE%\\.nod\\config.json in Notepad and
paste your key between the quotes" fails at every step for someone who has never
opened PowerShell: they cannot find the folder, Notepad saves it as
config.json.txt, and one stray quote produces a JSONDecodeError they have no way
to read. PyQt6 is already being shipped for the overlay, so a dialog costs no
bytes and removes all three failures.

Two windows, deliberately not a wizard:

  SetupDialog    everything on one page, nothing mandatory except the key
  DoctorDialog   the check-up report, with a Copy button -- "copy this and send
                 it to me" is the whole support loop for a remote tester

Both are plain Qt widgets over the data in `doctor.py` and `config.py`, which
stay Qt-free so they can be tested headless.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from . import calibrate, config, doctor, models

KEY_URL = "https://aistudio.google.com/apikey"


class _Worker(QThread):
    """Anything slow, off the UI thread. Qt freezes visibly otherwise."""

    done = pyqtSignal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.done.emit(exc)


class SetupDialog(QDialog):
    """First run: the API key, the microphone, and what Nod is allowed to do."""

    def __init__(self, args=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set up Nod")
        self.setMinimumWidth(560)
        self.cfg = config.load()
        self._workers: list[QThread] = []

        layout = QVBoxLayout(self)

        intro = QLabel(
            "<b>Nod listens to your microphone for “hey Nod”.</b><br>"
            "It can also read your screen and summarise meetings, but both of "
            "those are off until you turn them on below.<br><br>"
            "Everything is saved on this computer only.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # --- API key ---
        self.key = QLineEdit(self.cfg.get("gemini_api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("paste your free Google AI key here")
        get_key = QPushButton("Get a free key")
        get_key.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(KEY_URL)))
        self.test_btn = QPushButton("Test")
        self.test_btn.clicked.connect(self._test_key)
        row = QHBoxLayout()
        row.addWidget(self.key, 1)
        row.addWidget(get_key)
        row.addWidget(self.test_btn)
        form.addRow("Google AI key", row)

        self.key_status = QLabel("")
        self.key_status.setWordWrap(True)
        form.addRow("", self.key_status)

        # --- microphone ---
        self.mic = QComboBox()
        self._fill_microphones()
        form.addRow("Microphone", self.mic)

        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        cal = QPushButton("Calibrate")
        cal.clicked.connect(self._calibrate)
        row = QHBoxLayout()
        row.addWidget(self.level, 1)
        row.addWidget(cal)
        form.addRow("Microphone level", row)

        self.cal_status = QLabel("Press Calibrate if Nod cannot hear you.")
        self.cal_status.setWordWrap(True)
        form.addRow("", self.cal_status)

        layout.addLayout(form)

        # --- optional features ---
        self.meetings = QCheckBox(
            "Listen to my meetings and summarise them")
        self.meetings.setChecked(not self.cfg.get("no_summary", True))
        self.meetings.setToolTip(
            "Uses your daily Google quota, and downloads a larger model the "
            "first time.")
        layout.addWidget(self.meetings)

        self.screen = QCheckBox("Read what is on my screen")
        has_tesseract = doctor.find_tesseract() is not None
        self.screen.setChecked(
            has_tesseract and not self.cfg.get("no_vision", True))
        self.screen.setEnabled(has_tesseract)
        if not has_tesseract:
            self.screen.setText(
                "Read what is on my screen  —  needs Tesseract installed")
        layout.addWidget(self.screen)

        note = QLabel(
            "<i>Your key is stored in plain text in your user folder, the same "
            "way your browser stores logins. Anyone who can use your Windows "
            "account can read it.</i>")
        note.setWordWrap(True)
        note.setStyleSheet("color: #777;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- helpers ---------------------------------------------------------
    def _fill_microphones(self) -> None:
        try:
            import soundcard as sc

            names = [m.name for m in sc.all_microphones()]
            default = sc.default_microphone().name
        except Exception:
            names, default = [], None

        self.mic.addItem("Use Windows' default microphone", None)
        for name in names:
            self.mic.addItem(name, name)

        saved = self.cfg.get("mic_device")
        target = saved or default
        if target:
            index = self.mic.findData(target)
            if index >= 0:
                self.mic.setCurrentIndex(index)

    def _spawn(self, fn, on_done) -> None:
        worker = _Worker(fn)
        worker.done.connect(on_done)
        worker.finished.connect(lambda: self._workers.remove(worker))
        self._workers.append(worker)
        worker.start()

    def _test_key(self) -> None:
        key = self.key.text().strip()
        if not key:
            self.key_status.setText("Paste a key first.")
            return
        self.test_btn.setEnabled(False)
        self.key_status.setText("Checking…")

        def probe():
            import requests

            r = requests.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key}, timeout=8)
            return r.status_code

        def finished(result):
            self.test_btn.setEnabled(True)
            if isinstance(result, Exception):
                self.key_status.setText(
                    "Could not reach Google. Check your internet.")
            elif result == 200:
                self.key_status.setText("✓ The key works.")
            elif result in (400, 401, 403):
                self.key_status.setText(
                    "✗ Google rejected that key. Copy it again from the "
                    "website — it should be a long string with no spaces.")
            elif result == 429:
                self.key_status.setText(
                    "The key is valid but out of quota for today.")
            else:
                self.key_status.setText(f"Unexpected reply from Google "
                                        f"(HTTP {result}).")

        self._spawn(probe, finished)

    def _calibrate(self) -> None:
        device = self.mic.currentData()
        self.cal_status.setText("Stay quiet for three seconds…")

        def measure():
            quiet = calibrate.sample(3.0, device)
            return ("quiet", quiet)

        def after_quiet(result):
            if isinstance(result, Exception):
                self.cal_status.setText(f"Could not use that microphone: {result}")
                return
            _, quiet = result
            self.cal_status.setText("Now say “hey Nod” a few times…")

            def measure_loud():
                return ("loud", quiet, calibrate.sample(4.0, device))

            self._spawn(measure_loud, after_loud)

        def after_loud(result):
            if isinstance(result, Exception):
                self.cal_status.setText(f"Could not use that microphone: {result}")
                return
            _, quiet, loud = result
            start, stop, problem = calibrate.suggest(quiet, loud)
            if problem:
                self.cal_status.setText(problem)
                return
            self.cfg["start_rms"] = start
            self.cfg["stop_rms"] = stop
            peak = max(loud) if loud else 0.0
            self.level.setValue(min(100, int(peak * 2000)))
            self.cal_status.setText(
                f"✓ Set for this microphone (trigger level {start:.3f}).")

        self._spawn(measure, after_quiet)

    def _save(self) -> None:
        self.cfg["gemini_api_key"] = self.key.text().strip()
        self.cfg["mic_device"] = self.mic.currentData()
        self.cfg["no_summary"] = not self.meetings.isChecked()
        self.cfg["no_vision"] = not self.screen.isChecked()
        if self.screen.isChecked():
            found = doctor.find_tesseract()
            if found:
                self.cfg["tesseract"] = found
        # A model on disk before the first "hey Nod" is the difference between
        # a two-minute silence and none. Downloaded here, where it can show
        # progress, rather than lazily on a worker thread where it cannot.
        wake = self.cfg.get("wake_model", config.DEFAULTS["wake_model"])
        if not models.present(wake):
            self._download(wake)
        config.save(self.cfg)
        self.accept()

    def _download(self, name: str) -> None:
        from PyQt6.QtWidgets import QProgressDialog

        total = models.EXPECTED_MB.get(name, 150)
        box = QProgressDialog(
            f"Downloading Nod's ears ({total} MB). This happens once.",
            None, 0, total, self)
        box.setWindowTitle("Setting up Nod")
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setAutoClose(True)
        box.show()

        def progress(done_mb, total_mb):
            box.setValue(int(done_mb))

        def work():
            return models.ensure(name, progress=progress)

        def finished(_result):
            box.close()

        worker = _Worker(work)
        worker.done.connect(finished)
        self._workers.append(worker)
        worker.start()
        while worker.isRunning():
            from PyQt6.QtWidgets import QApplication

            QApplication.processEvents()
            worker.wait(50)


class DoctorDialog(QDialog):
    """The check-up report, in the only place a packaged build can show it."""

    def __init__(self, checks, args=None, allow_continue=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nod check-up")
        self.setMinimumSize(620, 520)
        self.checks = checks
        self._args = args

        layout = QVBoxLayout(self)

        fails, warns = doctor.summary(checks)
        headline = QLabel(
            "<b>Nod is ready.</b>" if not fails else
            f"<b>{fails} thing{'s' if fails != 1 else ''} need"
            f"{'' if fails != 1 else 's'} fixing before Nod will work "
            f"properly.</b>")
        headline.setWordWrap(True)
        layout.addWidget(headline)

        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setFont(QFont("Consolas", 9))
        self.report.setPlainText(doctor.as_text(checks))
        layout.addWidget(self.report, 1)

        hint = QLabel(
            "If you are stuck, press <b>Copy report</b> and send it to whoever "
            "gave you Nod, along with the file <code>nod.log</code> in your "
            "user folder.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        copy = QPushButton("Copy report")
        copy.clicked.connect(self._copy)
        settings = QPushButton("Settings…")
        settings.clicked.connect(self._settings)
        recheck = QPushButton("Check again")
        recheck.clicked.connect(self._recheck)
        row.addWidget(copy)
        row.addWidget(settings)
        row.addWidget(recheck)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox()
        if allow_continue:
            buttons.addButton("Start Nod anyway",
                              QDialogButtonBox.ButtonRole.AcceptRole)
            buttons.addButton("Quit", QDialogButtonBox.ButtonRole.RejectRole)
            buttons.rejected.connect(lambda: (self.reject(),
                                              __import__("sys").exit(1)))
        else:
            buttons.addButton("Close", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(doctor.as_text(self.checks))

    def _settings(self) -> None:
        SetupDialog(self._args, self).exec()
        self._recheck()

    def _recheck(self) -> None:
        config.apply_env()
        self.checks = doctor.run(quick=False)
        doctor.write_report(self.checks)
        self.report.setPlainText(doctor.as_text(self.checks))
