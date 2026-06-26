from __future__ import absolute_import, division, print_function, unicode_literals

from calibre.customize import InterfaceActionBase


class LocalPdfOcrBase(InterfaceActionBase):
    """Calibre wrapper class.

    Keep Qt imports out of this file so Calibre can inspect and install the
    plugin without loading the GUI action module.
    """

    name = "LocalPdfOcr"
    description = "Convert scanned PDFs to reflowable EPUBs with a local OpenAI-compatible vision LLM."
    supported_platforms = ["windows", "osx", "linux"]
    author = "Local PDF OCR contributors"
    version = (0, 1, 7)
    minimum_calibre_version = (5, 0, 0)
    actual_plugin = "calibre_plugins.local_pdf_ocr.ui:LocalPdfOcrAction"

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.local_pdf_ocr.config import ConfigWidget

        return ConfigWidget(self.actual_plugin_)

    def save_settings(self, config_widget):
        config_widget.save_settings()
        ac = self.actual_plugin_
        if ac is not None:
            ac.apply_settings()
