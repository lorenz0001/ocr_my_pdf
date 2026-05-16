import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

LANGUAGES = [
    ("Italiano", "ita"),
    ("Inglese", "eng"),
    ("Italiano + Inglese", "ita+eng"),
    ("Spagnolo", "spa"),
    ("Francese", "fra"),
    ("Tedesco", "deu"),
    ("Portoghese", "por"),
]

# Evita che si aprano finestre cmd a comparsa quando compilato senza console
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def to_wsl_path(win_path):
    res = subprocess.run(
        ["wsl", "wslpath", "-a", str(win_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=SUBPROCESS_FLAGS,
    )
    if res.returncode != 0 or not res.stdout.strip():
        err = res.stderr.strip() or "path non valido"
        raise RuntimeError(f"Errore conversione path WSL ({win_path}): {err}")
    return res.stdout.strip()


class OcrWorker(QObject):
    progress = Signal(int)
    status = Signal(str)
    log = Signal(str)
    finished = Signal(str, str)
    failed = Signal(str)

    def __init__(self, input_path, temp_output, lang="ita", total_pages=0, force_ocr=False):
        super().__init__()
        self.input_path = input_path
        self.temp_output = temp_output
        self.lang = lang
        self.total_pages = total_pages
        self.force_ocr = force_ocr
        self._abort = False
        self._proc = None

    def stop(self):
        self._abort = True
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    def _parse_progress(self, line):
        m_pct = re.search(r"(\d{1,3})%", line)
        if m_pct:
            val = int(m_pct.group(1))
            self.progress.emit(max(0, min(100, val)))
            return

        m_page = re.search(r"(?:page|Page)\s+(\d+)", line)
        if m_page and self.total_pages > 0:
            cur = int(m_page.group(1))
            pct = int(10 + (min(cur, self.total_pages) / self.total_pages) * 85)
            self.progress.emit(max(0, min(95, pct)))

    def run(self):
        try:
            self.status.emit("Conversione percorsi WSL...")
            in_wsl = to_wsl_path(self.input_path)
            out_wsl = to_wsl_path(self.temp_output)

            self.progress.emit(5)
            self.log.emit(f"File input:  {in_wsl}")
            self.log.emit(f"File temp:   {out_wsl}")
            self.log.emit(f"Lingua OCR:  {self.lang}")

            mode_flag = "--force-ocr" if self.force_ocr else "--skip-text"
            cmd = [
                "wsl",
                "ocrmypdf",
                "-l",
                self.lang,
                mode_flag,
                "--output-type",
                "pdf",
                in_wsl,
                out_wsl,
            ]

            self.log.emit(f"\n> {' '.join(cmd)}\n")
            self.status.emit("Elaborazione OCR in corso...")
            self.progress.emit(10)

            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=SUBPROCESS_FLAGS,
            )

            for raw_line in self._proc.stdout:
                if self._abort:
                    break
                line = raw_line.rstrip()
                if line:
                    self.log.emit(line)
                    self._parse_progress(line)

            if self._abort:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                self.failed.emit("Operazione interrotta.")
                return

            ret = self._proc.wait()
            if ret != 0:
                self.failed.emit(f"ocrmypdf terminato con codice {ret}. Controlla il log per i dettagli.")
                return

            self.progress.emit(100)
            self.status.emit("Completato")
            self.finished.emit(self.temp_output, self.input_path)

        except Exception as e:
            self.failed.emit(str(e))
        finally:
            self._proc = None


class DropArea(QFrame):
    fileSelected = Signal(str)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setObjectName("DropArea")
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(170)

        self.lbl_main = QLabel("Trascina qui un file PDF")
        self.lbl_main.setObjectName("DropMain")
        self.lbl_sub = QLabel("oppure clicca per sfogliare dal computer")
        self.lbl_sub.setObjectName("DropSub")
        self.lbl_file = QLabel("Nessun documento selezionato")
        self.lbl_file.setObjectName("DropFile")
        self.lbl_file.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(6)
        lay.addStretch()
        lay.addWidget(self.lbl_main, alignment=Qt.AlignCenter)
        lay.addWidget(self.lbl_sub, alignment=Qt.AlignCenter)
        lay.addSpacing(6)
        lay.addWidget(self.lbl_file, alignment=Qt.AlignCenter)
        lay.addStretch()

    def update_file(self, path):
        if not path:
            self.lbl_file.setText("Nessun documento selezionato")
            return
        p = Path(path)
        mb = p.stat().st_size / (1024 * 1024)
        self.lbl_file.setText(f"{p.name} ({mb:.2f} MB)")

    def _set_drag_state(self, active):
        self.setProperty("dragging", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                if u.isLocalFile() and u.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    self._set_drag_state(True)
                    return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drag_state(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drag_state(False)
        if not event.mimeData().hasUrls():
            return
        for u in event.mimeData().urls():
            if u.isLocalFile():
                fpath = u.toLocalFile()
                if fpath.lower().endswith(".pdf"):
                    self.fileSelected.emit(fpath)
                    event.acceptProposedAction()
                    return


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DropOCR")
        self.resize(820, 620)

        self.current_pdf = None
        self.temp_out = None
        self.thread = None
        self.worker = None

        self._build_ui()
        self._setup_theme()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_lay = QVBoxLayout(central)
        main_lay.setContentsMargins(18, 18, 18, 18)
        main_lay.setSpacing(12)

        # Barra superiore
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        lbl_lang = QLabel("Lingua:")
        self.cmb_lang = QComboBox()
        self.cmb_lang.setEditable(True)
        for label, code in LANGUAGES:
            self.cmb_lang.addItem(f"{label} ({code})", code)
        self.cmb_lang.setCurrentIndex(0)
        self.cmb_lang.setToolTip("Seleziona una lingua o inserisci il codice Tesseract (es. ita, eng, fra)")

        self.chk_force = QCheckBox("Forza OCR su tutte le pagine")
        self.chk_force.setToolTip("Se disattivato, salta le pagine che contengono già testo (--skip-text)")

        top_bar.addWidget(lbl_lang)
        top_bar.addWidget(self.cmb_lang)
        top_bar.addSpacing(8)
        top_bar.addWidget(self.chk_force)
        top_bar.addStretch()

        self.btn_open = QPushButton("Apri PDF...")
        self.btn_open.clicked.connect(self.browse_pdf)

        self.btn_cancel = QPushButton("Interrompi")
        self.btn_cancel.setObjectName("BtnCancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_job)

        self.btn_start = QPushButton("Avvia OCR")
        self.btn_start.setObjectName("BtnStart")
        self.btn_start.setEnabled(False)
        self.btn_start.clicked.connect(self.start_job)

        top_bar.addWidget(self.btn_open)
        top_bar.addWidget(self.btn_cancel)
        top_bar.addWidget(self.btn_start)
        main_lay.addLayout(top_bar)

        # Area drag & drop
        self.drop_area = DropArea()
        self.drop_area.fileSelected.connect(self.load_pdf)
        self.drop_area.clicked.connect(self.browse_pdf)
        main_lay.addWidget(self.drop_area)

        # Riga info documento e stato
        info_bar = QHBoxLayout()
        self.lbl_doc = QLabel("File: -")
        self.lbl_pages = QLabel("Pagine: -")
        self.lbl_status = QLabel("Pronto")
        self.lbl_doc.setObjectName("InfoText")
        self.lbl_pages.setObjectName("InfoText")
        self.lbl_status.setObjectName("InfoText")

        info_bar.addWidget(self.lbl_doc)
        info_bar.addStretch()
        info_bar.addWidget(self.lbl_pages)
        info_bar.addStretch()
        info_bar.addWidget(self.lbl_status)
        main_lay.addLayout(info_bar)

        # Progress bar
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        main_lay.addWidget(self.progress)

        # Output console
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setObjectName("LogView")
        self.txt_log.setPlaceholderText("Output di ocrmypdf...")
        main_lay.addWidget(self.txt_log, stretch=1)

    def _setup_theme(self):
        app = QApplication.instance()
        if app:
            pal = QPalette()
            pal.setColor(QPalette.Window, QColor("#1e1e1e"))
            pal.setColor(QPalette.WindowText, QColor("#e0e0e0"))
            pal.setColor(QPalette.Base, QColor("#141414"))
            pal.setColor(QPalette.AlternateBase, QColor("#252526"))
            pal.setColor(QPalette.Text, QColor("#e0e0e0"))
            pal.setColor(QPalette.Button, QColor("#2d2d30"))
            pal.setColor(QPalette.ButtonText, QColor("#e0e0e0"))
            pal.setColor(QPalette.Highlight, QColor("#0078d4"))
            pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
            app.setPalette(pal)

        self.setStyleSheet("""
            QMainWindow {
                background-color: #1e1e1e;
            }
            QLabel {
                color: #cccccc;
            }
            QCheckBox {
                color: #cccccc;
                spacing: 6px;
            }
            QComboBox {
                min-width: 170px;
                background-color: #252526;
                color: #e0e0e0;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
                padding: 5px 8px;
            }
            QComboBox:disabled {
                color: #777777;
                background-color: #1e1e1e;
            }
            QPushButton {
                background-color: #2d2d30;
                color: #e0e0e0;
                border: 1px solid #3e3e42;
                border-radius: 4px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: #3e3e42;
            }
            QPushButton:disabled {
                background-color: #252526;
                color: #666666;
                border-color: #2d2d30;
            }
            #BtnStart {
                background-color: #0e639c;
                color: #ffffff;
                border: 1px solid #1177bb;
                font-weight: 600;
            }
            #BtnStart:hover {
                background-color: #1177bb;
            }
            #BtnStart:disabled {
                background-color: #252526;
                color: #666666;
                border-color: #2d2d30;
            }
            #BtnCancel {
                background-color: #6e2222;
                color: #f0d0d0;
                border: 1px solid #8c2d2d;
            }
            #BtnCancel:hover {
                background-color: #852929;
            }
            #BtnCancel:disabled {
                background-color: #252526;
                color: #666666;
                border-color: #2d2d30;
            }
            #DropArea {
                background-color: #252526;
                border: 2px dashed #3e3e42;
                border-radius: 6px;
            }
            #DropArea[dragging="true"] {
                border-color: #0078d4;
                background-color: #1f2a33;
            }
            #DropMain {
                font-size: 16px;
                font-weight: 600;
                color: #e8e8e8;
            }
            #DropSub {
                font-size: 12px;
                color: #8c8c8c;
            }
            #DropFile {
                font-size: 13px;
                color: #cccccc;
                background-color: #1e1e1e;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 6px 12px;
                min-width: 260px;
                qproperty-alignment: AlignCenter;
            }
            #InfoText {
                color: #9e9e9e;
                font-size: 12px;
            }
            #LogView {
                background-color: #141414;
                border: 1px solid #2d2d30;
                border-radius: 4px;
                padding: 8px;
                color: #d4d4d4;
                font-family: Consolas, monospace;
                font-size: 12px;
            }
            QProgressBar {
                border: 1px solid #333333;
                border-radius: 4px;
                background-color: #141414;
                text-align: center;
                color: #e0e0e0;
                height: 18px;
            }
            QProgressBar::chunk {
                background-color: #0e639c;
                border-radius: 3px;
            }
        """)

    def browse_pdf(self):
        start_dir = str(Path(self.current_pdf).parent) if self.current_pdf else str(Path.home())
        fpath, _ = QFileDialog.getOpenFileName(self, "Seleziona PDF", start_dir, "File PDF (*.pdf)")
        if fpath:
            self.load_pdf(fpath)

    def load_pdf(self, fpath):
        p = Path(fpath)
        if not p.is_file() or p.suffix.lower() != ".pdf":
            QMessageBox.warning(self, "File non valido", "Seleziona un file PDF valido.")
            return

        self.current_pdf = str(p)
        self.drop_area.update_file(self.current_pdf)
        self.lbl_doc.setText(f"File: {p.name}")
        self.lbl_status.setText("Pronto")
        self.progress.setValue(0)
        self.txt_log.clear()

        pages = self._get_page_count(self.current_pdf)
        self.lbl_pages.setText(f"Pagine: {pages}" if pages else "Pagine: n/d")

        self.btn_start.setEnabled(True)
        self.log(f"Caricato: {self.current_pdf}")

    def _get_page_count(self, fpath):
        if fitz is None:
            return 0
        try:
            with fitz.open(fpath) as doc:
                return doc.page_count
        except Exception:
            return 0

    def _get_lang_code(self):
        idx = self.cmb_lang.currentIndex()
        text = self.cmb_lang.currentText().strip()
        if idx >= 0 and text == self.cmb_lang.itemText(idx):
            return self.cmb_lang.itemData(idx)
        # Caso in cui l'utente abbia scritto manualmente il codice (es. "deu+eng")
        m = re.search(r"\(([a-z_+]+)\)$", text)
        if m:
            return m.group(1)
        return text or "ita"

    def _set_busy(self, busy):
        self.btn_open.setEnabled(not busy)
        self.btn_start.setEnabled(not busy and bool(self.current_pdf))
        self.drop_area.setEnabled(not busy)
        self.cmb_lang.setEnabled(not busy)
        self.chk_force.setEnabled(not busy)
        self.btn_cancel.setEnabled(busy)

    def start_job(self):
        if not self.current_pdf or self.thread is not None:
            return

        lang = self._get_lang_code()
        in_path = Path(self.current_pdf)
        tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_"))
        self.temp_out = str(tmp_dir / f"{in_path.stem}_ocr.pdf")
        pages = self._get_page_count(self.current_pdf)

        self.txt_log.clear()
        self.log(f"Avvio lavoro su: {in_path.name}")
        self.lbl_status.setText("Avvio...")
        self.progress.setValue(2)
        self._set_busy(True)

        self.thread = QThread(self)
        self.worker = OcrWorker(
            input_path=self.current_pdf,
            temp_output=self.temp_out,
            lang=lang,
            total_pages=pages,
            force_ocr=self.chk_force.isChecked(),
        )
        self.worker.moveToThread(self.thread)

        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(self.lbl_status.setText)
        self.worker.log.connect(self.log)
        self.worker.failed.connect(self._on_error)
        self.worker.finished.connect(self._on_done)

        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)

        self.thread.start()

    def cancel_job(self):
        if self.worker:
            self.log("Interruzione richiesta...")
            self.lbl_status.setText("Interruzione...")
            self.btn_cancel.setEnabled(False)
            self.worker.stop()

    def log(self, msg):
        self.txt_log.appendPlainText(msg)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_error(self, err_msg):
        self.log(f"\n[ERRORE] {err_msg}")
        self.lbl_status.setText("Errore")
        self.progress.setValue(0)
        self._set_busy(False)
        self._remove_temp(self.temp_out)
        QMessageBox.critical(self, "Errore OCR", err_msg)

    def _on_done(self, temp_output, orig_input):
        self.log("\nOCR terminato con successo.")
        self.lbl_status.setText("Salvataggio file...")
        self.progress.setValue(100)

        orig = Path(orig_input)
        default_dest = orig.parent / f"{orig.stem}_ocr.pdf"

        dest_path, _ = QFileDialog.getSaveFileName(
            self,
            "Salva PDF con OCR",
            str(default_dest),
            "File PDF (*.pdf)",
        )

        if not dest_path:
            self.log("Salvataggio annullato.")
            self.lbl_status.setText("Annullato")
            self._set_busy(False)
            self._remove_temp(temp_output)
            return

        if not dest_path.lower().endswith(".pdf"):
            dest_path += ".pdf"

        try:
            dest = Path(dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            shutil.move(temp_output, str(dest))
            self.log(f"Salvato in: {dest}")
            self.lbl_status.setText("Pronto")
            QMessageBox.information(self, "Completato", f"File salvato correttamente:\n{dest}")
        except Exception as e:
            self._on_error(f"Impossibile salvare il file: {e}")
            return
        finally:
            self._set_busy(False)
            self._remove_temp(temp_output)

    def _remove_temp(self, temp_file):
        if not temp_file:
            return
        try:
            parent = Path(temp_file).parent
            if parent.exists() and parent.name.startswith("ocr_"):
                shutil.rmtree(parent, ignore_errors=True)
        except OSError:
            pass

    def _cleanup_thread(self):
        if self.thread:
            self.thread.deleteLater()
        if self.worker:
            self.worker.deleteLater()
        self.thread = None
        self.worker = None


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("DropOCR")
    app.setFont(QFont("Segoe UI", 9))

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
