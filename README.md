# Local PDF OCR Calibre Plugin

This repository contains a Calibre interface plugin that converts scanned PDFs to reflowable EPUBs by calling a local OpenAI-compatible vision LLM endpoint.

The plugin is intended for scanned/image PDFs. It renders each page, asks a local vision model for structured OCR JSON, then writes a standard EPUB back into the same Calibre book record as a new `EPUB` format.

Default endpoint:

```text
http://10.130.92.107:8000/v1
```

Default page parallelism is `4`.

## Install

Build the plugin ZIP:

```bash
python3 build_calibre_plugin.py
```

Install `dist/LocalPdfOcr.zip` in Calibre:

Preferences -> Plugins -> Load plugin from file.

After upgrading the plugin, restart Calibre so the new plugin module is loaded.

## Workflow

1. Select one Calibre book that has a PDF format.
2. Click `Local PDF OCR`.
3. If the PDF appears to contain a text layer, the plugin asks whether OCR should continue.
4. The plugin renders PDF pages through Calibre's bundled tools, preferring `pdftoppm` and falling back to Calibre conversion.
5. A worker sends page images to the configured `/v1/chat/completions` endpoint.
6. Page requests run concurrently according to `Parallel pages`.
7. Streaming deltas are shown in the progress dialog.
8. The dialog keeps only the latest configured number of pages in the OCR output box.
9. OCR results are written incrementally to a recovery cache.
10. If a page request times out, the model returns an error, or the response is not valid JSON, the worker retries automatically and then asks whether to retry or cancel the job.
11. The job plans an EPUB table of contents, writes a standard EPUB, and adds it back to the selected Calibre book as the `EPUB` format.
12. If the selected book already has an EPUB format, the plugin asks whether to replace it.

## Backend Design

The implementation is split into small modules:

- `config.py`: Calibre `JSONConfig` settings and customization UI.
- `local_llm.py`: OpenAI-compatible client, model discovery, vision requests, and streaming.
- `pdf_ocr_core.py`: PDF page rendering through Calibre's own converter, cover selection, page OCR orchestration, JSON parsing.
- `epub_writer.py`: EPUB3 packaging, cover writing, paragraph cleanup, illustration cropping.
- `ui.py`: Calibre action, progress dialog, cancel button, and worker thread.

## Recovery Cache

Recovery files are stored under Calibre's plugin config directory:

```text
<calibre config>/plugins/LocalPdfOcr/recovery/
```

If a job fails or Calibre is restarted, opening the same PDF again prompts whether to continue the previous job or restart from scratch.

For release builds, recovery cache is removed after a successful EPUB output by default. Enable `Keep OCR recovery cache after successful EPUB output` in the plugin settings when debugging.

## Cover And Illustration Strategy

Cover selection asks the vision model to choose from the first configurable number of pages. If detection fails or is disabled, page 1 is used.

For each page, the OCR prompt asks the model to return strict JSON:

```json
{
  "text": "page text",
  "page_role": "body",
  "has_illustrations": false,
  "illustrations": [
    {"bbox": [0.1, 0.2, 0.8, 0.3], "caption": "", "insert_after": ""}
  ]
}
```

Illustration `bbox` values are normalized to the page image. The EPUB writer crops those regions and inserts them after the paragraph containing `insert_after`, or at the nearest page location when no anchor is supplied.

## Notes

The cancel button requests interruption. In-flight HTTP requests cannot always be killed immediately by the server, so cancellation may wait until the current page request returns or times out.

Model failures are recoverable by design. A failed page does not end the whole conversion automatically; the plugin waits for the user to choose `Retry` or `Cancel Job`. Choosing `Retry` keeps attempting the same page.

This plugin intentionally does not depend on Tesseract, PaddleOCR, or OCRmyPDF.

It also does not require Homebrew `poppler` tools. PDF page images are produced through the Calibre installation that is already running the plugin.

## License

MIT
