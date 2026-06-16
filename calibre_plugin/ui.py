from __future__ import absolute_import, division, print_function, unicode_literals

import json
import hashlib
import shutil
import tempfile
import threading
import traceback
from collections import OrderedDict
from pathlib import Path

from calibre.constants import config_dir
from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from calibre_plugins.local_pdf_ocr.config import get_prefs
from calibre_plugins.local_pdf_ocr import epub_writer, pdf_ocr_core
from qt.core import (
    QDialog,
    QHBoxLayout,
    QIcon,
    QLabel,
    QMessageBox,
    QPixmap,
    QProgressBar,
    QPushButton,
    QTextCursor,
    QTextEdit,
    QThread,
    QVBoxLayout,
    pyqtSignal,
)


PLUGIN_ICONS = ["images/icon.png"]


class OcrProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent, recent_page_limit):
        QDialog.__init__(self, parent)
        self.recent_page_limit = max(1, int(recent_page_limit or 6))
        self.page_text = OrderedDict()
        self.setWindowTitle("Local PDF OCR")
        self.setMinimumWidth(760)
        self.setMinimumHeight(520)

        self.label = QLabel("Preparing OCR job...")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.output = QTextEdit()
        self.output.setReadOnly(True)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._request_cancel)
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("Recent OCR output"))
        layout.addWidget(self.output, 2)
        layout.addWidget(QLabel("Job log"))
        layout.addWidget(self.log, 1)
        layout.addLayout(buttons)
        self.setLayout(layout)

    def set_status(self, text):
        self.label.setText(str(text))

    def set_progress(self, current, total):
        total = max(int(total or 0), 1)
        current = max(0, min(int(current or 0), total))
        self.progress.setRange(0, total)
        self.progress.setValue(current)

    def append_log(self, text):
        self.log.append(str(text))
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def page_started(self, page):
        self._touch_page(page)
        self.page_text[page] += "[page {0}] ".format(page)
        self._refresh_output()

    def page_delta(self, page, text):
        self._touch_page(page)
        self.page_text[page] += str(text)
        self._refresh_output()

    def page_done(self, page, text):
        self._touch_page(page)
        self.page_text[page] = "[page {0} done]\n{1}".format(page, str(text or "").strip())
        self._refresh_output()

    def finish(self, message=None):
        if message:
            self.set_status(message)
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)

    def _touch_page(self, page):
        page = int(page)
        if page not in self.page_text:
            self.page_text[page] = ""
        self.page_text.move_to_end(page)
        while len(self.page_text) > self.recent_page_limit:
            self.page_text.popitem(last=False)

    def _refresh_output(self):
        parts = []
        for page in sorted(self.page_text):
            parts.append(self.page_text[page])
        should_follow = self._is_output_at_bottom()
        self.output.setPlainText("\n\n".join(parts))
        if should_follow:
            self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _is_output_at_bottom(self):
        bar = self.output.verticalScrollBar()
        if not bar:
            return True
        return bar.value() >= max(0, bar.maximum() - 4)

    def _request_cancel(self):
        self.cancel_button.setEnabled(False)
        self.append_log("Cancel requested. Stopping the current job...")
        self.cancel_requested.emit()

    def closeEvent(self, event):
        if self.close_button.isEnabled():
            event.accept()
        else:
            event.ignore()


class PdfOcrWorker(QThread):
    status = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    page_started_signal = pyqtSignal(int)
    page_delta_signal = pyqtSignal(int, str)
    page_done_signal = pyqtSignal(int, str)
    retry_needed = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, pdf_path, output_dir, title, authors, settings):
        QThread.__init__(self)
        self.pdf_path = Path(pdf_path)
        self.output_dir = Path(output_dir)
        self.title = title
        self.authors = list(authors or [])
        self.settings = dict(settings)
        self._cancel = False
        self._retry_condition = threading.Condition()
        self._retry_decisions = {}

    def cancel(self):
        self._cancel = True
        with self._retry_condition:
            for key in list(self._retry_decisions):
                if self._retry_decisions[key] is None:
                    self._retry_decisions[key] = "abandon"
            self._retry_condition.notify_all()

    def is_cancelled(self):
        return self._cancel

    def set_retry_decision(self, page, attempt, decision):
        key = (int(page), int(attempt))
        with self._retry_condition:
            self._retry_decisions[key] = str(decision or "retry")
            self._retry_condition.notify_all()

    def request_retry_decision(self, page, attempt, error):
        auto_retries = max(0, int(self.settings.get("auto_retry_attempts", 3) or 0))
        if int(page) != -2 and int(attempt) <= auto_retries:
            if int(page) == -1:
                target = "TOC planning"
            elif int(page) == 0:
                target = "cover selection"
            else:
                target = "page {0}".format(page)
            self.log.emit(
                "{0} attempt {1} failed or timed out. Automatically retrying ({1}/{2}).".format(
                    target,
                    int(attempt),
                    auto_retries,
                )
            )
            return "retry"
        key = (int(page), int(attempt))
        with self._retry_condition:
            self._retry_decisions[key] = None
        self.retry_needed.emit(int(page), int(attempt), str(error))
        with self._retry_condition:
            while self._retry_decisions.get(key) is None and not self._cancel:
                self._retry_condition.wait(0.5)
            return self._retry_decisions.pop(key, "abandon")

    def run(self):
        temp_dir = Path(tempfile.mkdtemp(prefix="local-pdf-ocr-"))
        try:
            render_dir = temp_dir / "pages"
            self.status.emit("Rendering PDF pages...")

            def render_progress(page, total, message):
                if self.is_cancelled():
                    raise RuntimeError("Canceled by user")
                self.log.emit(message)
                self.progress.emit(page, total)

            image_paths = pdf_ocr_core.render_pdf_pages(
                self.pdf_path,
                render_dir,
                progress=render_progress,
                cancel_callback=self.is_cancelled,
            )
            total_pages = len(image_paths)
            self.log.emit("Rendered {0} page image(s).".format(total_pages))

            self.output_dir.mkdir(parents=True, exist_ok=True)
            recovery_path = Path(self.settings.get("_recovery_cache_path") or self.output_dir / (epub_writer.safe_name(self.title, self.pdf_path.stem) + "_ocr_results.json"))
            recovery_path.parent.mkdir(parents=True, exist_ok=True)
            resume_cache_path = self.settings.get("_resume_cache_path")
            cached_by_page = {}
            if resume_cache_path:
                self.status.emit("Loading OCR recovery cache...")
                cached = self._load_recovery_cache(resume_cache_path, image_paths, allow_partial=True)
                cover_page = int(cached.get("cover_page") or 1)
                for result in cached.get("results") or []:
                    try:
                        cached_by_page[int(result.get("page") or 0)] = result
                    except Exception:
                        pass
                self.progress.emit(len(cached_by_page), total_pages)
                self.log.emit("Loaded OCR recovery cache: {0}".format(resume_cache_path))
                self.log.emit("Recovered {0} OCR page result(s).".format(len(cached_by_page)))
            else:
                self.status.emit("Choosing cover...")
                cover_page, cover_reason = pdf_ocr_core.choose_cover_page(
                    image_paths,
                    self.settings,
                    cancel_callback=self.is_cancelled,
                    retry_callback=self.request_retry_decision,
                )
                self.log.emit("Cover page: {0}. {1}".format(cover_page, cover_reason))
                self._write_recovery_cache(recovery_path, total_pages, cover_page, [])

            results_by_page = dict(cached_by_page)
            missing_items = [
                (index, image_path)
                for index, image_path in enumerate(image_paths, 1)
                if index not in results_by_page
            ]
            if missing_items:
                self.status.emit("OCR pages with local vision LLM...")
                completed = {"count": len(results_by_page)}

                def page_started(page, total):
                    self.page_started_signal.emit(page)

                def page_delta(page, delta):
                    self.page_delta_signal.emit(page, delta)

                def page_done(result, total):
                    completed["count"] += 1
                    page = int(result.get("page") or completed["count"])
                    if 1 <= page <= total_pages:
                        results_by_page[page] = result
                    self._write_recovery_cache(
                        recovery_path,
                        total_pages,
                        cover_page,
                        [results_by_page[index] for index in range(1, total_pages + 1) if index in results_by_page],
                    )
                    self.page_done_signal.emit(page, result.get("text") or "")
                    self.progress.emit(completed["count"], total)
                    self.log.emit(
                        "Page {page}/{total}: {chars} chars, {seconds:.1f}s, role={role}".format(
                            page=page,
                            total=total,
                            chars=len(result.get("text") or ""),
                            seconds=float(result.get("seconds") or 0),
                            role=result.get("page_role") or "unknown",
                        )
                    )

                new_results = pdf_ocr_core.ocr_page_items(
                    missing_items,
                    total_pages,
                    self.settings,
                    page_started=page_started,
                    page_delta=page_delta,
                    page_done=page_done,
                    cancel_callback=self.is_cancelled,
                    retry_callback=self.request_retry_decision,
                )
                for result in new_results:
                    results_by_page[int(result.get("page") or 0)] = result
            else:
                self.log.emit("All OCR pages recovered from cache; skipping page OCR.")
            if self.is_cancelled():
                self.failed.emit("Canceled by user")
                return
            missing_pages = [index for index in range(1, total_pages + 1) if index not in results_by_page]
            if missing_pages:
                raise RuntimeError(
                    "OCR result cache is incomplete: {0}/{1} page(s); missing page(s): {2}".format(
                        len(results_by_page),
                        total_pages,
                        ", ".join(str(page) for page in missing_pages[:20]),
                    )
                )
            results = [results_by_page[index] for index in range(1, total_pages + 1)]

            self._write_recovery_cache(recovery_path, total_pages, cover_page, results)
            self.log.emit("OCR recovery cache written: {0}".format(recovery_path))

            self.status.emit("Planning EPUB table of contents...")
            self.progress.emit(0, 3)
            toc_plan = pdf_ocr_core.plan_toc(
                results,
                self.settings,
                cancel_callback=self.is_cancelled,
                retry_callback=self.request_retry_decision,
                status_callback=self.log.emit,
            )
            self.progress.emit(1, 3)
            self.log.emit("TOC planned with {0} item(s).".format(len(toc_plan.get("items") or [])))
            if toc_plan.get("chunk_count"):
                self.log.emit(
                    "TOC planning used {0} candidate(s) in {1} chunk(s).".format(
                        toc_plan.get("candidate_count") or "?",
                        toc_plan.get("chunk_count") or "?",
                    )
                )
            if toc_plan.get("fallback"):
                self.log.emit("TOC planning used fallback rules after LLM failure: {0}".format(toc_plan.get("fallback_reason") or ""))

            self.status.emit("Writing EPUB...")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.output_dir / (epub_writer.safe_name(self.title, self.pdf_path.stem) + ".epub")
            epub_writer.write_epub(
                output_path,
                self.title,
                self.authors,
                results,
                cover_image_path=image_paths[max(0, cover_page - 1)],
                cover_page=cover_page,
                toc_plan=toc_plan,
                progress_callback=self.log.emit,
            )
            self.progress.emit(2, 3)
            self.log.emit("EPUB written: {0}".format(output_path))
            if self.settings.get("keep_page_images"):
                kept = self.output_dir / (epub_writer.safe_name(self.title, self.pdf_path.stem) + "_pages")
                if kept.exists():
                    shutil.rmtree(str(kept))
                shutil.copytree(str(render_dir), str(kept))
                self.log.emit("Rendered page images kept: {0}".format(kept))
            if not self.settings.get("keep_recovery_cache", True):
                try:
                    shutil.rmtree(str(recovery_path.parent), ignore_errors=True)
                    self.log.emit("OCR recovery cache removed after successful output.")
                except Exception:
                    pass
            self.progress.emit(3, 3)
            self.finished_ok.emit({"epub_path": str(output_path), "page_count": total_pages, "cover_page": cover_page})
        except RuntimeError as exc:
            if "Canceled by user" in str(exc) or "Abandoned by user" in str(exc):
                self.failed.emit("Canceled by user")
            else:
                self.failed.emit(traceback.format_exc())
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            if not self.settings.get("keep_page_images"):
                shutil.rmtree(str(temp_dir), ignore_errors=True)

    def _write_recovery_cache(self, recovery_path, total_pages, cover_page, results):
        recovery_path = Path(recovery_path)
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "title": self.title,
            "authors": self.authors,
            "source_pdf": str(self.pdf_path),
            "pdf_fingerprint": self.settings.get("_pdf_fingerprint") or "",
            "page_count": int(total_pages),
            "cover_page": int(cover_page or 1),
            "completed_pages": len(results or []),
            "results": list(results or []),
        }
        temp_path = recovery_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(recovery_path)

    def _load_recovery_cache(self, recovery_path, image_paths, allow_partial=False):
        with Path(recovery_path).open("r", encoding="utf-8") as f:
            cached = json.load(f)
        results = cached.get("results") or []
        if not allow_partial and len(results) != len(image_paths):
            raise RuntimeError(
                "OCR recovery cache page count does not match the rendered PDF: cache={0}, rendered={1}".format(
                    len(results),
                    len(image_paths),
                )
            )
        image_by_page = {index: str(path) for index, path in enumerate(image_paths, 1)}
        for result in results:
            try:
                page = int(result.get("page") or 0)
            except Exception:
                page = 0
            if page in image_by_page:
                result["image_path"] = image_by_page[page]
        cached["results"] = results
        return cached


class LocalPdfOcrAction(InterfaceAction):
    name = "LocalPdfOcr"
    action_spec = (
        "Local PDF OCR",
        None,
        "Convert a scanned PDF to a reflowable EPUB with a local vision LLM.",
        None,
    )
    action_type = "global"

    def genesis(self):
        self._install_icon()
        self.qaction.triggered.connect(self.plugin_button)

    def apply_settings(self):
        pass

    def _install_icon(self):
        icon_data = self.load_resources(PLUGIN_ICONS).get("images/icon.png")
        if not icon_data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(icon_data):
            self.qaction.setIcon(QIcon(pixmap))

    def location_selected(self, loc):
        self.qaction.setEnabled(loc == "library")

    def plugin_button(self):
        context = self._selected_pdf_context()
        if context is None:
            return
        db, book_id, source_mi, pdf_path = context
        settings = get_prefs()
        fingerprint = self._pdf_fingerprint(pdf_path)
        default_recovery_path = self._recovery_cache_path(fingerprint, source_mi, pdf_path)
        recovery_path = self._find_recovery_cache(fingerprint, pdf_path) or default_recovery_path
        settings["_pdf_fingerprint"] = fingerprint
        settings["_recovery_cache_path"] = str(recovery_path if recovery_path.exists() else default_recovery_path)
        if recovery_path.exists():
            decision = self._confirm_recovery_cache(recovery_path)
            if decision == "cancel":
                return
            if decision == "resume":
                settings["_resume_cache_path"] = str(recovery_path)
                settings["_recovery_cache_path"] = str(recovery_path)
            elif decision == "restart":
                shutil.rmtree(str(recovery_path.parent), ignore_errors=True)
                settings["_recovery_cache_path"] = str(default_recovery_path)
        if settings.get("warn_text_pdf", True) and not self._confirm_text_pdf_ocr(pdf_path):
            return
        output_dir = Path(tempfile.mkdtemp(prefix="local-pdf-ocr-output-"))
        result = self._run_ocr_with_progress(pdf_path, output_dir, source_mi, settings)
        if result is None:
            if not list(output_dir.glob("*_ocr_results.json")):
                shutil.rmtree(str(output_dir), ignore_errors=True)
            return
        epub_path = Path(result["epub_path"])
        if db.format_abspath(book_id, "EPUB", index_is_id=True):
            if not self._confirm_replace_epub_format():
                info_dialog(
                    self.gui,
                    "Local PDF OCR",
                    "EPUB was created but not added to Calibre.",
                    det_msg=str(epub_path),
                    show=True,
                )
                return
        self._add_epub_format_to_book(db, book_id, epub_path)
        self.gui.library_view.model().refresh_ids([book_id])
        self.gui.library_view.select_rows([book_id])
        self.gui.tags_view.recount()
        if self.gui.cover_flow:
            self.gui.cover_flow.dataChanged()
        info_dialog(
            self.gui,
            "Local PDF OCR",
            "Added EPUB format to the selected book from {0} page(s).".format(result.get("page_count")),
            det_msg=str(epub_path),
            show=True,
        )
        shutil.rmtree(str(output_dir), ignore_errors=True)

    def _selected_pdf_context(self):
        selected_ids = list(self.gui.library_view.get_selected_ids())
        if len(selected_ids) != 1:
            error_dialog(self.gui, "Select One Book", "Please select exactly one PDF book.", show=True)
            return None
        db = self.gui.current_db
        book_id = selected_ids[0]
        pdf_abspath = db.format_abspath(book_id, "PDF", index_is_id=True)
        if not pdf_abspath:
            error_dialog(self.gui, "No PDF", "The selected book does not have a PDF format.", show=True)
            return None
        pdf_path = Path(pdf_abspath)
        if not pdf_path.exists():
            error_dialog(
                self.gui,
                "PDF Not Found",
                "Calibre reported a PDF format, but the file could not be found on disk.",
                det_msg=str(pdf_path),
                show=True,
            )
            return None
        return db, book_id, db.get_metadata(book_id, index_is_id=True), pdf_path

    def _recovery_root(self):
        return Path(config_dir) / "plugins" / "LocalPdfOcr" / "recovery"

    def _legacy_recovery_root(self):
        return Path(config_dir) / "local_pdf_ocr" / "recovery"

    def _pdf_fingerprint(self, pdf_path):
        pdf_path = Path(pdf_path)
        stat = pdf_path.stat()
        h = hashlib.sha256()
        h.update(str(pdf_path.resolve()).encode("utf-8", "ignore"))
        h.update(str(stat.st_size).encode("ascii"))
        h.update(str(int(stat.st_mtime)).encode("ascii"))
        with pdf_path.open("rb") as f:
            h.update(f.read(1024 * 1024))
            if stat.st_size > 1024 * 1024:
                f.seek(max(0, stat.st_size - 1024 * 1024))
                h.update(f.read(1024 * 1024))
        return h.hexdigest()

    def _recovery_cache_path(self, fingerprint, source_mi, pdf_path):
        title = source_mi.title or Path(pdf_path).stem
        dirname = "{0}-{1}".format(epub_writer.safe_name(title, Path(pdf_path).stem), str(fingerprint)[:16])
        return self._recovery_root() / dirname / "ocr_results.json"

    def _find_recovery_cache(self, fingerprint, pdf_path):
        self._migrate_legacy_recovery_cache()
        root = self._recovery_root()
        if not root.exists():
            return None
        short = str(fingerprint)[:16]
        for candidate in sorted(root.glob("*-{0}/ocr_results.json".format(short))):
            return candidate
        pdf_path = str(Path(pdf_path))
        for candidate in sorted(root.glob("*/ocr_results.json")):
            try:
                with candidate.open("r", encoding="utf-8") as f:
                    cached = json.load(f)
            except Exception:
                continue
            if cached.get("pdf_fingerprint") == fingerprint:
                return candidate
            if cached.get("source_pdf") == pdf_path:
                return candidate
        return None

    def _migrate_legacy_recovery_cache(self):
        legacy_root = self._legacy_recovery_root()
        new_root = self._recovery_root()
        if not legacy_root.exists():
            return
        for old_cache in legacy_root.glob("*/ocr_results.json"):
            try:
                relative = old_cache.relative_to(legacy_root)
            except Exception:
                continue
            new_cache = new_root / relative
            if new_cache.exists():
                continue
            try:
                new_cache.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(old_cache), str(new_cache))
            except Exception:
                pass

    def _confirm_recovery_cache(self, recovery_path):
        box = QMessageBox(self.gui)
        box.setWindowTitle("Local PDF OCR")
        box.setIcon(QMessageBox.Icon.Question if hasattr(QMessageBox, "Icon") else QMessageBox.Question)
        box.setText("检查到上次失败的工作，是否继续上次的工作？")
        box.setInformativeText("继续会使用上次已经完成的 OCR 结果，并从目录规划/EPUB 输出阶段继续。重新开始会清除这份缓存并重新 OCR。")
        try:
            stat = Path(recovery_path).stat()
            details = ["Cache file: {0}".format(recovery_path), "Size: {0} bytes".format(stat.st_size)]
            with Path(recovery_path).open("r", encoding="utf-8") as f:
                cached = json.load(f)
            completed = cached.get("completed_pages")
            if completed is None:
                completed = len(cached.get("results") or [])
            details.append("Completed pages: {0}/{1}".format(completed, cached.get("page_count") or "?"))
            details.append("Source PDF: {0}".format(cached.get("source_pdf") or ""))
            box.setDetailedText("\n".join(details))
        except Exception:
            box.setDetailedText("Cache file: {0}".format(recovery_path))
        button_role = getattr(QMessageBox, "ButtonRole", QMessageBox)
        resume_button = box.addButton("继续上次工作", button_role.AcceptRole)
        restart_button = box.addButton("重新开始", button_role.DestructiveRole if hasattr(button_role, "DestructiveRole") else button_role.ActionRole)
        cancel_button = box.addButton("取消", button_role.RejectRole)
        box.setDefaultButton(resume_button)
        box.setEscapeButton(cancel_button)
        box.exec_()
        clicked = box.clickedButton()
        if clicked == resume_button:
            return "resume"
        if clicked == restart_button:
            return "restart"
        return "cancel"

    def _confirm_text_pdf_ocr(self, pdf_path):
        try:
            analysis = pdf_ocr_core.detect_pdf_text_layer(pdf_path)
        except Exception:
            return True
        if not analysis.get("has_text_layer"):
            return True
        box = QMessageBox(self.gui)
        box.setWindowTitle("Local PDF OCR")
        box.setIcon(QMessageBox.Icon.Warning if hasattr(QMessageBox, "Icon") else QMessageBox.Warning)
        box.setText("This PDF appears to be text-heavy rather than scanned.")
        box.setInformativeText(
            "Local PDF OCR is intended for scanned/image PDFs. For text-heavy PDFs, normal Calibre conversion or text cleanup may be a better first step. Continue OCR anyway?"
        )
        details = [
            "Text objects: {0}".format(analysis.get("text_objects", 0)),
            "Text drawing operations: {0}".format(analysis.get("text_show_ops", 0)),
            "Estimated text bytes: {0}".format(analysis.get("text_bytes", 0)),
            "Image streams: {0}".format(analysis.get("image_streams", 0)),
            "Image draw operations: {0}".format(analysis.get("image_draw_ops", 0)),
            "Image-dominant: {0}".format(bool(analysis.get("image_dominant", False))),
            "Streams scanned: {0}".format(analysis.get("streams_scanned", 0)),
            "Streams decompressed: {0}".format(analysis.get("streams_decompressed", 0)),
            "Confidence: {0:.2f}".format(float(analysis.get("confidence", 0.0))),
            "Reason: {0}".format(analysis.get("reason", "")),
        ]
        if analysis.get("truncated"):
            details.append("Only the first part of the PDF was scanned for this quick check.")
        box.setDetailedText("\n".join(details))
        button_role = getattr(QMessageBox, "ButtonRole", QMessageBox)
        continue_button = box.addButton("Continue OCR", button_role.AcceptRole)
        cancel_button = box.addButton("Cancel OCR", button_role.RejectRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec_()
        return box.clickedButton() == continue_button

    def _confirm_replace_epub_format(self):
        box = QMessageBox(self.gui)
        box.setWindowTitle("Local PDF OCR")
        box.setIcon(QMessageBox.Icon.Warning if hasattr(QMessageBox, "Icon") else QMessageBox.Warning)
        box.setText("The selected book already has an EPUB format.")
        box.setInformativeText("Replace the existing EPUB with the newly generated OCR EPUB?")
        button_role = getattr(QMessageBox, "ButtonRole", QMessageBox)
        replace_button = box.addButton("Replace EPUB", button_role.AcceptRole)
        cancel_button = box.addButton("Keep Existing", button_role.RejectRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec_()
        return box.clickedButton() == replace_button

    def _run_ocr_with_progress(self, pdf_path, output_dir, source_mi, settings):
        title = source_mi.title or Path(pdf_path).stem
        authors = source_mi.authors or ["Unknown"]
        dialog = OcrProgressDialog(self.gui, int(settings.get("recent_page_buffer", 6)))
        worker = PdfOcrWorker(pdf_path, output_dir, title, authors, settings)
        state = {"result": None, "error": None}
        dialog.cancel_requested.connect(worker.cancel)
        worker.status.connect(dialog.set_status)
        worker.progress.connect(dialog.set_progress)
        worker.log.connect(dialog.append_log)
        worker.page_started_signal.connect(dialog.page_started)
        worker.page_delta_signal.connect(dialog.page_delta)
        worker.page_done_signal.connect(dialog.page_done)
        worker.retry_needed.connect(lambda page, attempt, error: self._handle_retry_needed(dialog, worker, page, attempt, error))
        worker.finished_ok.connect(lambda result: state.update(result=result))
        worker.finished_ok.connect(lambda result: dialog.finish("OCR job finished."))
        worker.failed.connect(lambda error: state.update(error=error))
        worker.failed.connect(lambda error: dialog.finish("OCR job stopped." if "Canceled by user" in str(error) else "OCR job failed."))
        worker.start()
        dialog.exec_()
        worker.wait()
        if state["error"]:
            if "Canceled by user" in state["error"]:
                return None
            error_dialog(
                self.gui,
                "PDF OCR Failed",
                "Local PDF OCR failed while converting the PDF.",
                det_msg=state["error"],
                show=True,
            )
            return None
        return state["result"]

    def _handle_retry_needed(self, dialog, worker, page, attempt, error):
        if page == -1:
            target = "TOC planning"
        elif page == -2:
            target = "TOC planning"
        elif page == 0:
            target = "cover selection"
        else:
            target = "page {0}".format(page)
        dialog.set_status("{0} is waiting for a retry decision...".format(target))
        dialog.append_log(
            "{0} attempt {1} failed or timed out. Waiting for Retry or Cancel Job.".format(target, attempt)
        )
        box = QMessageBox(dialog)
        box.setWindowTitle("Local PDF OCR")
        box.setIcon(QMessageBox.Icon.Warning if hasattr(QMessageBox, "Icon") else QMessageBox.Warning)
        if page == -2:
            box.setText("The local model still did not return a usable table of contents plan.")
        elif page == -1:
            box.setText("The local model did not return a usable table of contents plan.")
        elif page == 0:
            box.setText("The local model did not return a usable result for cover selection.")
        else:
            box.setText("The local model did not return a usable result for page {0}.".format(page))
        if page == -2:
            box.setInformativeText("Choose Use Fallback TOC to continue with rule-based headings, Retry to ask the model again, or Cancel Job to stop.")
        else:
            box.setInformativeText("Choose Retry to keep waiting and try this page again, or Cancel Job to stop the OCR job.")
        box.setDetailedText(str(error))
        button_role = getattr(QMessageBox, "ButtonRole", QMessageBox)
        fallback_button = None
        if page == -2:
            fallback_button = box.addButton("Use Fallback TOC", button_role.AcceptRole)
        retry_button = box.addButton("Retry", button_role.AcceptRole)
        abandon_button = box.addButton("Cancel Job", button_role.RejectRole)
        box.setDefaultButton(fallback_button or retry_button)
        box.exec_()
        clicked = box.clickedButton()
        if fallback_button is not None and clicked == fallback_button:
            dialog.append_log("Using fallback TOC after LLM planning failed.")
            worker.set_retry_decision(page, attempt, "fallback")
        elif clicked == abandon_button:
            if page == -1:
                dialog.append_log("User canceled the OCR job during TOC planning.")
            elif page == -2:
                dialog.append_log("User canceled the OCR job during TOC fallback decision.")
            elif page == 0:
                dialog.append_log("User abandoned the OCR job during cover selection.")
            else:
                dialog.append_log("User abandoned the OCR job at page {0}.".format(page))
            worker.set_retry_decision(page, attempt, "abandon")
        else:
            if page == -1:
                dialog.append_log("Retrying TOC planning after attempt {0}.".format(attempt))
            elif page == 0:
                dialog.append_log("Retrying cover selection after attempt {0}.".format(attempt))
            else:
                dialog.append_log("Retrying page {0} after attempt {1}.".format(page, attempt))
            worker.set_retry_decision(page, attempt, "retry")

    def _add_epub_format_to_book(self, db, book_id, epub_path):
        db.add_format_with_hooks(book_id, "EPUB", str(epub_path), index_is_id=True)
        db.commit()
