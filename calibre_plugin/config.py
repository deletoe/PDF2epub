from __future__ import absolute_import, division, print_function, unicode_literals

from calibre.utils.config import JSONConfig
from qt.core import QCheckBox, QFormLayout, QLineEdit, QSpinBox, QVBoxLayout, QWidget


DEFAULT_PREFS = {
    "prefs_version": 2,
    "base_url": "http://10.130.92.107:8000/v1",
    "model": "",
    "parallel_pages": 4,
    "request_timeout": 180,
    "auto_retry_attempts": 3,
    "max_tokens_per_page": 65536,
    "toc_max_tokens": 65536,
    "recent_page_buffer": 6,
    "cover_scan_pages": 5,
    "detect_cover": True,
    "detect_illustrations": True,
    "warn_text_pdf": True,
    "convert_traditional_to_simplified": False,
    "keep_recovery_cache": False,
    "keep_page_images": False,
}

prefs = JSONConfig("plugins/LocalPdfOcr")
prefs.defaults = DEFAULT_PREFS


def get_prefs():
    if int(prefs.get("prefs_version", 0) or 0) < 2:
        prefs["keep_recovery_cache"] = DEFAULT_PREFS["keep_recovery_cache"]
        prefs["prefs_version"] = DEFAULT_PREFS["prefs_version"]
    values = {}
    for key, default in DEFAULT_PREFS.items():
        values[key] = prefs.get(key, default)
    return values


class ConfigWidget(QWidget):
    def __init__(self, plugin_action):
        QWidget.__init__(self)
        self.plugin_action = plugin_action
        values = get_prefs()

        layout = QVBoxLayout()
        form = QFormLayout()
        layout.addLayout(form)
        layout.addStretch(1)
        self.setLayout(layout)

        self.base_url = QLineEdit(str(values["base_url"]))
        form.addRow("OpenAI-compatible base URL", self.base_url)

        self.model = QLineEdit(str(values["model"] or ""))
        self.model.setPlaceholderText("Leave empty to use the first /v1/models result")
        form.addRow("Model", self.model)

        self.parallel_pages = QSpinBox()
        self.parallel_pages.setRange(1, 8)
        self.parallel_pages.setValue(int(values["parallel_pages"]))
        form.addRow("Parallel pages", self.parallel_pages)

        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(30, 1800)
        self.request_timeout.setSingleStep(30)
        self.request_timeout.setValue(int(values["request_timeout"]))
        form.addRow("Page request timeout seconds", self.request_timeout)

        self.auto_retry_attempts = QSpinBox()
        self.auto_retry_attempts.setRange(0, 20)
        self.auto_retry_attempts.setValue(int(values["auto_retry_attempts"]))
        form.addRow("Automatic retries before asking", self.auto_retry_attempts)

        self.max_tokens_per_page = QSpinBox()
        self.max_tokens_per_page.setRange(512, 65536)
        self.max_tokens_per_page.setSingleStep(512)
        self.max_tokens_per_page.setValue(int(values["max_tokens_per_page"]))
        form.addRow("Max tokens per page", self.max_tokens_per_page)

        self.toc_max_tokens = QSpinBox()
        self.toc_max_tokens.setRange(1024, 131072)
        self.toc_max_tokens.setSingleStep(1024)
        self.toc_max_tokens.setValue(int(values["toc_max_tokens"]))
        form.addRow("TOC max tokens", self.toc_max_tokens)

        self.recent_page_buffer = QSpinBox()
        self.recent_page_buffer.setRange(1, 50)
        self.recent_page_buffer.setValue(int(values["recent_page_buffer"]))
        form.addRow("Displayed recent pages", self.recent_page_buffer)

        self.detect_cover = QCheckBox("Ask the local vision model to choose a cover from the first pages")
        self.detect_cover.setChecked(bool(values["detect_cover"]))
        form.addRow("", self.detect_cover)

        self.cover_scan_pages = QSpinBox()
        self.cover_scan_pages.setRange(1, 20)
        self.cover_scan_pages.setValue(int(values["cover_scan_pages"]))
        form.addRow("Cover scan pages", self.cover_scan_pages)

        self.detect_illustrations = QCheckBox("Ask for illustration ranges and insert cropped images into the EPUB")
        self.detect_illustrations.setChecked(bool(values["detect_illustrations"]))
        form.addRow("", self.detect_illustrations)

        self.warn_text_pdf = QCheckBox("Warn before OCR when the PDF appears to contain a text layer")
        self.warn_text_pdf.setChecked(bool(values["warn_text_pdf"]))
        form.addRow("", self.warn_text_pdf)

        self.convert_traditional_to_simplified = QCheckBox("Ask the model to OCR Traditional Chinese as precise Simplified Chinese")
        self.convert_traditional_to_simplified.setChecked(bool(values["convert_traditional_to_simplified"]))
        form.addRow("", self.convert_traditional_to_simplified)

        self.keep_recovery_cache = QCheckBox("Keep OCR recovery cache after successful EPUB output")
        self.keep_recovery_cache.setChecked(bool(values["keep_recovery_cache"]))
        form.addRow("", self.keep_recovery_cache)

        self.keep_page_images = QCheckBox("Keep rendered page images in the temporary job directory")
        self.keep_page_images.setChecked(bool(values["keep_page_images"]))
        form.addRow("", self.keep_page_images)

    def save_settings(self):
        prefs["prefs_version"] = DEFAULT_PREFS["prefs_version"]
        prefs["base_url"] = str(self.base_url.text()).strip() or DEFAULT_PREFS["base_url"]
        prefs["model"] = str(self.model.text()).strip()
        prefs["parallel_pages"] = int(self.parallel_pages.value())
        prefs["request_timeout"] = int(self.request_timeout.value())
        prefs["auto_retry_attempts"] = int(self.auto_retry_attempts.value())
        prefs["max_tokens_per_page"] = int(self.max_tokens_per_page.value())
        prefs["toc_max_tokens"] = int(self.toc_max_tokens.value())
        prefs["recent_page_buffer"] = int(self.recent_page_buffer.value())
        prefs["detect_cover"] = self.detect_cover.isChecked()
        prefs["cover_scan_pages"] = int(self.cover_scan_pages.value())
        prefs["detect_illustrations"] = self.detect_illustrations.isChecked()
        prefs["warn_text_pdf"] = self.warn_text_pdf.isChecked()
        prefs["convert_traditional_to_simplified"] = self.convert_traditional_to_simplified.isChecked()
        prefs["keep_recovery_cache"] = self.keep_recovery_cache.isChecked()
        prefs["keep_page_images"] = self.keep_page_images.isChecked()
