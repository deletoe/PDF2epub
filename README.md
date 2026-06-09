# Local PDF OCR Calibre Plugin

## 中文说明

Local PDF OCR 是一个 Calibre 界面插件，用本地 OpenAI-compatible 视觉大模型把扫描版 PDF 转换成可重排 EPUB。

插件面向扫描版/影印版 PDF：它会把 PDF 渲染成页面图片，请本地视觉模型返回结构化 OCR JSON，然后把生成的 EPUB 作为同一个 Calibre 书目下的新 `EPUB` 格式写回书库。

默认接口地址：

```text
http://10.130.92.107:8000/v1
```

默认页面并发数为 `4`。

### 安装

构建插件 ZIP：

```bash
python3 build_calibre_plugin.py
```

在 Calibre 中安装 `dist/LocalPdfOcr.zip`：

```text
首选项 -> 插件 -> 从文件加载插件
```

升级插件后，请重启 Calibre，让新版插件模块重新加载。

### 工作流程

1. 在 Calibre 中选择一本带有 PDF 格式的书目。
2. 点击 `Local PDF OCR`。
3. 如果插件判断 PDF 可能已有文本层，会询问是否继续 OCR。
4. 插件使用 Calibre 自带工具渲染 PDF 页面，优先使用 `pdftoppm`，必要时回退到 Calibre 转换流程。
5. Worker 将页面图片发送到配置的 `/v1/chat/completions` 接口。
6. 页面请求按 `Parallel pages` 设置并发执行。
7. 进度窗口会流式显示模型输出。
8. OCR 输出框只保留最近若干页，避免整本书输出挤满窗口。
9. OCR 结果会增量写入恢复缓存。
10. 如果页面请求超时、模型报错或返回非法 JSON，插件会先自动重试，再询问用户继续重试还是取消任务。
11. OCR 完成后，插件规划 EPUB 目录，写出标准 EPUB，并把它作为所选书目的 `EPUB` 格式加入 Calibre。
12. 如果所选书目已经有 EPUB 格式，插件会询问是否替换。

### 模块结构

- `config.py`：Calibre `JSONConfig` 设置和插件配置界面。
- `local_llm.py`：OpenAI-compatible 客户端、模型发现、视觉请求和流式响应。
- `pdf_ocr_core.py`：PDF 页面渲染、封面选择、页面 OCR 编排、JSON 解析和目录规划。
- `epub_writer.py`：EPUB3 打包、封面、正文、脚注、插图裁切和导航文件生成。
- `ui.py`：Calibre action、进度窗口、取消按钮和后台 worker 线程。

### 恢复缓存

恢复缓存保存在 Calibre 插件配置目录：

```text
<calibre config>/plugins/LocalPdfOcr/recovery/
```

如果任务失败或 Calibre 重启，再次打开同一个 PDF 时，插件会提示是否继续上次任务或重新开始。

正式版本默认在成功输出 EPUB 后清理恢复缓存。调试时可以在插件设置里启用 `Keep OCR recovery cache after successful EPUB output`。

### 封面和插图

封面选择会让视觉模型从前几页中挑选最适合作为 EPUB 封面的页面。检测失败或关闭检测时，默认使用第 1 页。

每页 OCR prompt 要求模型返回严格 JSON，例如：

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

插图 `bbox` 使用相对于整页图片的归一化 `xyxy` 坐标。EPUB writer 会裁切这些区域，并插入到包含 `insert_after` 的段落之后；没有锚点时会放到最接近的页面位置。

### 说明

取消按钮会请求中断任务。已经发出的 HTTP 请求不一定能被服务器立即停止，因此取消可能需要等待当前页面请求返回或超时。

模型错误是可恢复的。单页失败不会自动结束整本转换；插件会让用户选择 `Retry` 或 `Cancel Job`。选择 `Retry` 会继续尝试同一页。

本插件不依赖 Tesseract、PaddleOCR 或 OCRmyPDF，也不要求安装 Homebrew `poppler`。PDF 页面图片由当前运行的 Calibre 安装提供。

### 许可证

MIT

---

## English

Local PDF OCR is a Calibre interface plugin that converts scanned PDFs to reflowable EPUBs by calling a local OpenAI-compatible vision LLM endpoint.

The plugin is intended for scanned/image PDFs. It renders each page, asks a local vision model for structured OCR JSON, then writes a standard EPUB back into the same Calibre book record as a new `EPUB` format.

Default endpoint:

```text
http://10.130.92.107:8000/v1
```

Default page parallelism is `4`.

### Install

Build the plugin ZIP:

```bash
python3 build_calibre_plugin.py
```

Install `dist/LocalPdfOcr.zip` in Calibre:

```text
Preferences -> Plugins -> Load plugin from file
```

After upgrading the plugin, restart Calibre so the new plugin module is loaded.

### Workflow

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

### Backend Design

The implementation is split into small modules:

- `config.py`: Calibre `JSONConfig` settings and customization UI.
- `local_llm.py`: OpenAI-compatible client, model discovery, vision requests, and streaming.
- `pdf_ocr_core.py`: PDF page rendering, cover selection, page OCR orchestration, JSON parsing, and table-of-contents planning.
- `epub_writer.py`: EPUB3 packaging, cover writing, body rendering, footnotes, illustration cropping, and navigation files.
- `ui.py`: Calibre action, progress dialog, cancel button, and worker thread.

### Recovery Cache

Recovery files are stored under Calibre's plugin config directory:

```text
<calibre config>/plugins/LocalPdfOcr/recovery/
```

If a job fails or Calibre is restarted, opening the same PDF again prompts whether to continue the previous job or restart from scratch.

For release builds, recovery cache is removed after a successful EPUB output by default. Enable `Keep OCR recovery cache after successful EPUB output` in the plugin settings when debugging.

### Cover And Illustration Strategy

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

Illustration `bbox` values are normalized `xyxy` coordinates relative to the page image. The EPUB writer crops those regions and inserts them after the paragraph containing `insert_after`, or at the nearest page location when no anchor is supplied.

### Notes

The cancel button requests interruption. In-flight HTTP requests cannot always be killed immediately by the server, so cancellation may wait until the current page request returns or times out.

Model failures are recoverable by design. A failed page does not end the whole conversion automatically; the plugin waits for the user to choose `Retry` or `Cancel Job`. Choosing `Retry` keeps attempting the same page.

This plugin intentionally does not depend on Tesseract, PaddleOCR, or OCRmyPDF.

It also does not require Homebrew `poppler` tools. PDF page images are produced through the Calibre installation that is already running the plugin.

### License

MIT
