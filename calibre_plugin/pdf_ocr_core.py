from __future__ import absolute_import, division, print_function, unicode_literals

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from calibre_plugins.local_pdf_ocr.local_llm import LocalLlmClient, StreamRepetitionError


class InvalidJsonResponseError(RuntimeError):
    def __init__(self, message, response_text="", raw_response=None):
        RuntimeError.__init__(self, message)
        self.response_text = response_text or ""
        self.raw_response = raw_response or {}


PAGE_PROMPT = """请把这页中文扫描书页转写为可重排电子书文本，并返回严格 JSON。

要求：
1. 只输出页面中真实可见的文字，不要补写下一页或推测缺失内容。
1a. 如果页末是未完句、跨页断词或未完引文，按原页可见文字停住；不要为了句子完整而补标点、补字或补闭引号。
1b. 即使页面只有一个月份、章节题名、页签或极少量文字，也必须输出这些可见文字；不要返回空对象 {}。真正空白页也必须按 schema 返回 page_role="blank"、text=""、blocks=[]。如果页面只有插图、照片、地图、表格、手稿图等，没有可转写正文，也必须返回合法 JSON：text=""、blocks=[]、page_role="illustration" 或合适角色、has_illustrations=true，并在 illustrations 中给出图片范围。污迹、透印、扫描噪点或无法清楚辨认的残字不要猜测或补写。
2. 去掉页码、装饰线、页眉、页脚。页眉通常是每页顶部反复出现的书名/章节名/栏目名，例如“阿莱克修斯传”等；除非它也是本页正文中的真实标题，否则不要放入正文。
3. 合并同一自然段内的换行；对话另起段。
4. 使用中文标点。不要改写原文。
5. 不要在中文、数字、英文、标点之间额外添加空格；标题里的装饰性字间距要还原，例如“译 者 序”应输出为“译者序”，“1131 年”应输出为“1131年”。
6. text 字段必须包含本页完整可重排正文，标题用 Markdown #/##/### 标出。只有当本页确实没有可转写正文、只有插图/照片/图表时，text 才可以为空。blocks 字段只标注标题、引文、带注释锚点的段落、跨页续接的首段等结构信息；普通正文段落不要在 blocks 中重复一遍。
7. 如果本页第一段不是新段落，而是承接上一页最后一段，请把 page_continues_previous 设为 true，并把第一个正文 paragraph block 的 continued_from_previous 设为 true。
8. 如果页面边角或页边有角注、脚注、校注、译注等，不要混进正文；放入 notes，并在相关 block 中用 note_markers 标出锚点。无法确定锚点时 anchor 留空。
9. 如果页面包含插图、照片、地图、表格、手稿图等非纯装饰图片，在 illustrations 中给出裁切范围。
10. bbox 必须使用 0 到 1 的归一化 xyxy 坐标：[x1, y1, x2, y2]，相对于整页左上角；x2/y2 是右下角坐标，不是宽高。
11. insert_after 使用应插入图片之前的最短原文片段；如果图片应置于页首，则留空。

返回 JSON 结构：
{
  "text": "Markdown 预览文本：标题用 #/##/###，段落之间用两个换行分隔，不包含页眉页脚",
  "page_continues_previous": false,
  "page_role": "cover|front_matter|toc|body|illustration|blank|copyright|unknown",
  "title": "本页明显标题，没有则为空",
  "blocks": [
    {"type": "heading", "level": 2, "text": "译者序", "note_markers": []},
    {"type": "paragraph", "text": "正文段落", "continued_from_previous": false, "note_markers": ["1"]},
    {"type": "quote", "text": "引文段落", "continued_from_previous": false, "note_markers": []}
  ],
  "has_illustrations": false,
  "illustrations": [
    {"bbox": [0.1, 0.2, 0.9, 0.5], "caption": "", "insert_after": ""}
  ],
  "notes": [
    {"marker": "1", "type": "footnote|margin_note|translator_note|editor_note|unknown", "text": "注释文字", "anchor": "正文中的短锚点"}
  ]
}
"""


SIMPLIFIED_OUTPUT_PROMPT = """\n\n额外语言要求：
1. 如果原页是繁体中文或旧译名，请在 OCR 输出阶段直接转写为现代简体中文；不要先输出繁体再说明转换。
2. 转写必须逐句对应原文，不要概括、删节、增补或改写事实。繁体转简体只改变字形、常用词和通行译名，不改变句子结构和含义。
3. 常用词和译名按大陆简体通行写法输出，例如：电脑软体/電腦軟體->电脑软件，资料/資料->数据，资讯/資訊->信息，网路/網路->网络，程式->程序，档案/檔案->文件，史大林/史達林->斯大林，史密司->史密斯，苏维埃/蘇維埃->苏维埃，苏联/蘇聯->苏联，罗马/羅馬->罗马，俄国/俄國->俄国。
4. 人名、书名、地名如已有通行简体译名，使用通行译名；没有把握时只做繁简字形转换，保持原文可追溯。
5. “著”作为作者署名时保留为“著”，不要机械改成“着”；只有“着着失利”等现代简体语境才写作“着”。
"""


COMPACT_PAGE_JSON_PROMPT = """\n\n紧凑 JSON 输出要求：
1. text 仍然输出本页完整可重排正文。
2. 普通正文段落不要逐段复制到 blocks；blocks 只保留 heading、quote、带 note_markers 的 paragraph、或 continued_from_previous=true 的首段。
3. 如果没有这些特殊结构，blocks 可以为空数组，后续流程会从 text 自动拆段。
4. notes 和 illustrations 仍按原 schema 输出；没有则为空数组。
5. 只输出一个完整 JSON 对象，不要 Markdown 代码块，不要解释。
"""


PLAIN_TEXT_RECOVERY_PROMPT = """请重新观察这页扫描图，只转写页面中真实可见的正文文字。

要求：
1. 只输出纯文本，不要 JSON，不要 Markdown 代码块，不要解释。
2. 去掉页码、页眉、页脚和装饰线。
3. 合并同一自然段内的换行，段落之间用一个空行分隔。
4. 如果上一轮一直输出空白、回车、/n、\\n 或不完整 JSON，请不要延续上一轮输出，必须重新转写图片。
5. 如果页面确实没有可转写正文，只输出空字符串。

上一轮错误：
{error}

上一轮输出摘要：
{response}
"""


def compact_previous_response_for_prompt(value, limit=4000):
    text = str(value or "")
    text = re.sub(r"([ \t\r\n])\1{8,}", r"\1[repeated whitespace omitted]\1", text)
    text = re.sub(r"((?:/n|\\n)[\"'`,，,\s]*){8,}", "[repeated /n output omitted]", text)
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return head + "\n...[previous response truncated]...\n" + tail


def build_page_retry_prompt(error_message, previous_response):
    return (
        "\n\n上一次 OCR 尝试失败了。请重新观察图片并修正你的输出，仍然只返回一个完整 JSON 对象，不要 Markdown 代码块，不要解释。\n"
        "不要延续上一次错误输出；如果上一次一直输出 /n、\\n、重复符号或不完整 JSON，那是无效结果，请完全重新生成。\n"
        "这一次必须返回单行压缩 JSON：不要使用 ```json 代码块，不要漂亮打印，不要在 JSON 字符串中输出真实换行；段落换行必须写成转义的 \\n\\n。\n"
        "如果上一次 JSON 中已经能看到正文、注释或 illustrations，请尽量保留这些可见信息，但修正 schema、引号、逗号、page_role、has_illustrations 和 bbox。\n"
        "如果页面只有图片没有正文，这不是错误：返回 text=\"\"、blocks=[]、page_role=\"illustration\" 或合适角色、has_illustrations=true、illustrations=[...]。\n\n"
        "上一次错误：\n{error}\n\n"
        "上一次原始输出：\n{response}\n"
    ).format(
        error=str(error_message or "")[:2400],
        response=compact_previous_response_for_prompt(previous_response, 4000),
    )


def build_page_prompt(settings):
    prompt = PAGE_PROMPT
    if settings and settings.get("convert_traditional_to_simplified", False):
        prompt += SIMPLIFIED_OUTPUT_PROMPT
    if not settings or not settings.get("_disable_compact_page_json"):
        prompt += COMPACT_PAGE_JSON_PROMPT
    if settings and (settings.get("_page_retry_error") or settings.get("_page_retry_response")):
        prompt += build_page_retry_prompt(
            settings.get("_page_retry_error") or "",
            settings.get("_page_retry_response") or "",
        )
    return prompt


COVER_PROMPT = """请判断这页是否适合作为 EPUB 封面。返回严格 JSON：
{"is_cover": true, "cover_quality": 0.82, "reason": "简短原因"}

判断优先级：真实封面 > 扉页 > 彩页/题名页。纯正文页返回 false。
cover_quality 在 0~1 之间，越大越可能是封面。
"""


COVER_MULTI_PROMPT = """请从这些候选页面中选择最适合作为 EPUB 封面的页面。返回严格 JSON：
{"cover_page": 1, "reason": "简短原因"}

页面编号从 1 开始，对应输入图片顺序。优先选择真实封面，其次选择扉页；不要选择纯正文页。
"""


SEMANTIC_REPAIR_PROMPT = """请根据相邻扫描页图片和已有 OCR 结果，只返回语义修补补丁，不要重新转写全文。

目标：
1. 判断已有 OCR 中哪些 heading/title 实际是重复页眉，应删除。
2. 保留真正新出现的章节标题、小标题。
3. 修正 page_continues_previous 和第一个正文 block 的 continued_from_previous。
4. 不要改写已经正确的正文文字；除非是删除页眉、合并明显误拆标题、修正注释结构。
5. 必须保留已有 notes；如果图片中可见注释而已有 OCR 漏掉，可新增 note，但不要因为修补页眉而删除注释。
6. 只输出严格 JSON。

输入图片顺序与 pages 数组顺序一致。

补丁格式：
{
  "repairs": [
    {
      "page": 7,
      "set_title": "",
      "set_page_continues_previous": true,
      "delete_block_indices": [0],
      "set_block_continued_from_previous": [{"block_index": 0, "value": true}],
      "notes": "keep",
      "reason": "第0个 heading 是连续页顶部重复页眉"
    }
  ]
}

如果某页不需要修补，不要为它输出 repair。
"""


TOC_PROMPT = """请根据 OCR 后的标题候选，规划 EPUB 目录结构。只输出严格 JSON。

要求：
1. 目录只包含读者需要导航到的正文结构、前言/序言/目录/作者序/译者序/章节/附录等。
2. 不要包含封面、书名页、版权页、CIP 数据、重复书名、样本文件标题、纯装饰页眉。
3. 如果一个大标题下面有“一、二、三、四、五”等小标题，应把这些小标题作为它的子级。
4. 保留候选中的 page 和 block_index，label 可做少量清理，level 使用 1~4。
5. 不要编造候选列表中不存在的目录项。

返回格式：
{
  "items": [
    {"page": 5, "block_index": 0, "label": "译者序", "level": 1, "reason": "序言主标题"},
    {"page": 5, "block_index": 1, "label": "一、1131年，君士坦丁堡的故事", "level": 2, "reason": "译者序下的小节"}
  ]
}
"""


def find_command(name):
    path = shutil.which(name)
    if path:
        return path
    executable = Path(sys.executable)
    candidates = [
        executable.with_name(name),
        executable.with_name(name + ".exe"),
    ]
    for parent in [executable.parent] + list(executable.parents):
        candidates.extend(
            [
                parent / "utils.app" / "Contents" / "MacOS" / name,
                parent / "utils.app" / "Contents" / "MacOS" / (name + ".exe"),
                parent / "MacOS" / name,
                parent / "MacOS" / (name + ".exe"),
            ]
        )
    candidates.extend(
        [
            Path("/Applications/calibre.app/Contents/utils.app/Contents/MacOS") / name,
            Path("/Applications/calibre.app/Contents/MacOS") / name,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def require_command(name):
    path = find_command(name)
    if path:
        return path
    raise RuntimeError("Required Calibre command not found: {0}".format(name))


def pdf_page_count(pdf_path):
    raise RuntimeError("Page count is determined after Calibre renders the PDF.")


def natural_key(path):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", Path(path).name)]


def page_count_from_pdfinfo(pdf_path):
    command = find_command("pdfinfo")
    if not command:
        return None
    try:
        output = subprocess.check_output(
            [command, str(pdf_path)],
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=20,
        )
    except Exception:
        return None
    match = re.search(r"^Pages:\s*(\d+)\s*$", output, re.MULTILINE)
    if match:
        return int(match.group(1))
    return None


def detect_pdf_text_layer(pdf_path, max_scan_bytes=96 * 1024 * 1024, max_streams=240):
    pdf_path = Path(pdf_path)
    data = pdf_path.read_bytes()
    truncated = False
    if len(data) > max_scan_bytes:
        data = data[:max_scan_bytes]
        truncated = True

    stats = {
        "has_text_layer": False,
        "confidence": 0.0,
        "text_objects": 0,
        "text_show_ops": 0,
        "text_bytes": 0,
        "image_streams": 0,
        "image_draw_ops": 0,
        "image_dominant": False,
        "streams_scanned": 0,
        "streams_decompressed": 0,
        "truncated": truncated,
        "reason": "",
    }

    def score_content(content):
        text_objects = len(re.findall(br"\bBT\b", content))
        show_ops = len(re.findall(br"(?:\)|\]|\>)\s*(?:Tj|TJ|'|\")\b", content))
        literal_text = 0
        for match in re.finditer(br"\((?:\\.|[^\\()]){2,}\)\s*(?:Tj|'|\")\b", content):
            literal_text += max(0, len(match.group(0)) - 4)
        for match in re.finditer(br"<[0-9A-Fa-f\s]{8,}>\s*Tj\b", content):
            literal_text += max(0, len(match.group(0)) - 4) // 2
        return text_objects, show_ops, literal_text

    def add_score(content):
        text_objects, show_ops, literal_text = score_content(content)
        stats["text_objects"] += text_objects
        stats["text_show_ops"] += show_ops
        stats["text_bytes"] += literal_text

    add_score(data)
    for match in re.finditer(br"\bstream\r?\n", data):
        if stats["streams_scanned"] >= max_streams:
            break
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            break
        raw = data[start:end].strip(b"\r\n")
        header = data[max(0, match.start() - 2048) : match.start()]
        stats["streams_scanned"] += 1
        if b"/Subtype" in header and b"/Image" in header:
            stats["image_streams"] += 1
        if b"FlateDecode" in header:
            try:
                content = zlib.decompress(raw)
                stats["streams_decompressed"] += 1
            except Exception:
                continue
        else:
            content = raw
        stats["image_draw_ops"] += len(re.findall(br"/[A-Za-z0-9_.:-]+\s+Do\b", content))
        add_score(content)

    score = stats["text_show_ops"] * 2 + stats["text_objects"] + min(stats["text_bytes"] // 80, 40)
    stats["confidence"] = min(0.98, score / 120.0)
    embedded_text = (
        stats["text_show_ops"] >= 30
        or stats["text_objects"] >= 20
        or stats["text_bytes"] >= 1200
    )
    stats["image_dominant"] = (
        stats["image_streams"] >= 20
        and (
            stats["image_draw_ops"] >= 20
            or stats["image_streams"] >= max(20, stats["text_objects"] // 4)
        )
    )
    stats["has_text_layer"] = embedded_text and not stats["image_dominant"]
    if stats["has_text_layer"]:
        stats["reason"] = (
            "Found {0} text objects and {1} text drawing operations, with only {2} image streams."
        ).format(stats["text_objects"], stats["text_show_ops"], stats["image_streams"])
    elif embedded_text and stats["image_dominant"]:
        stats["reason"] = (
            "Found embedded text, but the PDF is image-dominant ({0} image streams, {1} image draws), so it still looks like a scanned PDF."
        ).format(stats["image_streams"], stats["image_draw_ops"])
    else:
        stats["reason"] = (
            "Found only {0} text objects and {1} text drawing operations; this looks like a scanned PDF."
        ).format(stats["text_objects"], stats["text_show_ops"])
    return stats


def render_pdf_pages(pdf_path, out_dir, dpi=220, progress=None, cancel_callback=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdftoppm = find_command("pdftoppm")
    if pdftoppm:
        return render_pdf_pages_with_pdftoppm(
            pdf_path,
            out_dir,
            pdftoppm,
            dpi=dpi,
            progress=progress,
            cancel_callback=cancel_callback,
        )
    return render_pdf_pages_with_ebook_convert(
        pdf_path,
        out_dir,
        dpi=dpi,
        progress=progress,
        cancel_callback=cancel_callback,
    )


def start_throttled_output_reader(stream, prefix="", report_every=1000):
    state = {"last_message": "", "line_count": 0, "suppressed": 0, "report": ""}
    lock = threading.Lock()

    def read_output():
        if not stream:
            return
        for output_line in stream:
            message = str(output_line or "").strip()
            if not message:
                continue
            with lock:
                state["last_message"] = message
                state["line_count"] += 1
                state["suppressed"] += 1
                if state["suppressed"] >= report_every:
                    state["report"] = "{0}{1} renderer warning line(s) suppressed; latest: {2}".format(
                        prefix,
                        state["line_count"],
                        message[:180],
                    )
                    state["suppressed"] = 0

    reader = threading.Thread(target=read_output)
    reader.daemon = True
    reader.start()

    def consume_report():
        with lock:
            report = state.get("report") or ""
            state["report"] = ""
            return report

    def last_message():
        with lock:
            return state.get("last_message") or ""

    return consume_report, last_message


def render_pdf_pages_with_pdftoppm(pdf_path, out_dir, command, dpi=220, progress=None, cancel_callback=None):
    out_dir = Path(out_dir)
    render_dir = out_dir / "pdftoppm_pages"
    if render_dir.exists():
        shutil.rmtree(str(render_dir))
    render_dir.mkdir(parents=True, exist_ok=True)
    page_count = page_count_from_pdfinfo(pdf_path)
    prefix = render_dir / "page"
    cmd = [command, "-r", str(int(dpi)), "-jpeg", str(pdf_path), str(prefix)]
    if progress:
        progress(0, page_count or 1, "Rendering PDF pages with Calibre pdftoppm...")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    last_rendered = -1
    consume_report, last_message = start_throttled_output_reader(process.stdout, "Calibre pdftoppm: ")
    while True:
        if cancel_callback and cancel_callback():
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            raise RuntimeError("Canceled by user")
        time.sleep(0.2)
        report = consume_report()
        if progress and report:
            progress(max(0, last_rendered), page_count or 1, report)
        rendered_count = len(list(render_dir.glob("page-*.jpg")))
        if rendered_count != last_rendered and rendered_count > 0:
            last_rendered = rendered_count
            if progress:
                if page_count:
                    progress(rendered_count, page_count, "Rendered page {0}/{1}".format(rendered_count, page_count))
                else:
                    progress(rendered_count, max(1, rendered_count), "Rendered page {0}".format(rendered_count))
        if process.poll() is not None:
            break
    if process.returncode:
        raise RuntimeError("Calibre pdftoppm failed while rendering PDF pages.\n{0}".format(last_message()))

    images = sorted(render_dir.glob("page-*.jpg"), key=natural_key)
    if not images:
        raise RuntimeError("Calibre pdftoppm did not produce page images.")
    rendered = []
    total = len(images)
    for page_number, image_path in enumerate(images, 1):
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        final_path = out_dir / "page_{0:04d}.jpg".format(page_number)
        shutil.copyfile(str(image_path), str(final_path))
        rendered.append(final_path)
        if progress:
            progress(page_number, total, "Rendered page {0}/{1}".format(page_number, total))
    return rendered


def render_pdf_pages_with_ebook_convert(pdf_path, out_dir, dpi=220, progress=None, cancel_callback=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    oeb_dir = out_dir / "calibre_oeb"
    if oeb_dir.exists():
        shutil.rmtree(str(oeb_dir))
    command = require_command("ebook-convert")
    cmd = [command, str(pdf_path), str(oeb_dir), "--verbose"]
    if progress:
        progress(0, 1, "Rendering PDF pages with Calibre ebook-convert...")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    consume_report, last_message = start_throttled_output_reader(process.stdout, "Calibre PDF renderer: ")
    while True:
        if cancel_callback and cancel_callback():
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            raise RuntimeError("Canceled by user")
        time.sleep(0.2)
        report = consume_report()
        if progress and report:
            progress(0, 1, report)
        if process.poll() is not None:
            break
    if process.returncode:
        raise RuntimeError("Calibre ebook-convert failed while rendering PDF pages.\n{0}".format(last_message()))

    candidates = []
    for pattern in ("*.jpg", "*.jpeg", "*.png"):
        candidates.extend(oeb_dir.glob(pattern))
    images = [
        path
        for path in sorted(candidates, key=natural_key)
        if not path.name.lower().startswith("cover")
    ]
    if not images:
        raise RuntimeError("Calibre ebook-convert did not produce page images.")

    rendered = []
    page_count = len(images)
    for page_number, image_path in enumerate(images, 1):
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        suffix = ".jpg" if image_path.suffix.lower() in (".jpg", ".jpeg") else ".png"
        final_path = out_dir / "page_{0:04d}{1}".format(page_number, suffix)
        shutil.copyfile(str(image_path), str(final_path))
        rendered.append(final_path)
        if progress:
            progress(page_number, page_count, "Rendered page {0}/{1}".format(page_number, page_count))
    return rendered


def parse_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        extracted = text[start : end + 1]
        if extracted != text:
            candidates.append(extracted)
    for candidate in list(candidates):
        repaired = repair_json_object_text(candidate)
        if repaired != candidate:
            candidates.append(repaired)
    last_error = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("No JSON object found")


def escape_control_chars_in_json_strings(text):
    text = str(text or "")
    chars = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                chars.append(char)
                escaped = False
                continue
            if char == "\\":
                chars.append(char)
                escaped = True
                continue
            if char == '"':
                chars.append(char)
                in_string = False
                continue
            if char == "\n":
                chars.append("\\n")
                continue
            if char == "\r":
                chars.append("\\r")
                continue
            if char == "\t":
                chars.append("\\t")
                continue
            if ord(char) < 32:
                continue
            chars.append(char)
            continue
        chars.append(char)
        if char == '"':
            in_string = True
            escaped = False
    return "".join(chars)


def repair_json_object_text(text):
    text = str(text or "")
    # Common LLM typo: an object key gains one extra quote, e.g.
    # ,""page_role": "body". Restrict the repair to JSON key positions.
    text = re.sub(r'([{\[,]\s*)""([A-Za-z_][A-Za-z0-9_]*)"\s*:', r'\1"\2":', text)
    text = escape_control_chars_in_json_strings(text)
    return text


def extract_json_string_value(text, key):
    text = str(text or "")
    match = re.search(r'"{0}"\s*:'.format(re.escape(key)), text)
    if not match:
        return ""
    index = match.end()
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] != '"':
        return ""
    index += 1
    chars = []
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(chars)
        if char == "\\" and index + 1 < len(text):
            index += 1
            escaped = text[index]
            if escaped == "n":
                chars.append("\n")
            elif escaped == "r":
                chars.append("\r")
            elif escaped == "t":
                chars.append("\t")
            elif escaped == "b":
                chars.append("\b")
            elif escaped == "f":
                chars.append("\f")
            elif escaped == "u" and index + 4 < len(text):
                code = text[index + 1 : index + 5]
                try:
                    chars.append(chr(int(code, 16)))
                    index += 4
                except Exception:
                    chars.append("\\u" + code)
            else:
                chars.append(escaped)
        else:
            chars.append(char)
        index += 1
    return "".join(chars)


def extract_json_bool_value(text, key, default=False):
    match = re.search(r'"{0}"\s*:\s*(true|false)'.format(re.escape(key)), str(text or ""), re.I)
    if not match:
        return default
    return match.group(1).lower() == "true"


def cleanup_ocr_text(text):
    text = str(text or "").strip()
    # Vision models sometimes preserve decorative title spacing or add spaces
    # around numbers in Chinese text. Keep ordinary English spaces intact.
    cjk = r"\u3400-\u4dbf\u4e00-\u9fff"
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"([{0}])[ \t]+([{0}])".format(cjk), r"\1\2", text)
        text = re.sub(r"([{0}])[ \t]+([0-9A-Za-z])".format(cjk), r"\1\2", text)
        text = re.sub(r"([0-9A-Za-z])[ \t]+([{0}])".format(cjk), r"\1\2", text)
    text = re.sub(r"[ \t]+([，。！？；：、）】》])", r"\1", text)
    text = re.sub(r"([（【《])[ \t]+", r"\1", text)
    return text


def normalize_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def vision_max_image_side(settings):
    try:
        return int((settings or {}).get("vision_max_image_side", 2400) or 0)
    except Exception:
        return 2400


def cover_vision_max_image_side(settings):
    try:
        return int((settings or {}).get("cover_vision_max_image_side", 1200) or 0)
    except Exception:
        return 1200


def is_fatal_vision_preprocess_error(error):
    return "LLM vision preprocessing failed" in str(error or "")


def compact_heading_key(text):
    return re.sub(r"\s+", "", cleanup_ocr_text(text or ""))


def text_coverage_length(text):
    return len(re.sub(r"\s+", "", cleanup_ocr_text(text or "")))


def rebuild_blocks_from_text_with_structural_hints(blocks, fallback_text):
    heading_hints = {}
    quote_hints = set()
    continued_first = False
    first_note_markers = []
    for index, block in enumerate(blocks or []):
        block_text = cleanup_ocr_text(block.get("text") or "")
        key = compact_heading_key(block_text)
        if not key:
            continue
        if index == 0 and normalize_bool(block.get("continued_from_previous")):
            continued_first = True
            first_note_markers = [str(x).strip() for x in (block.get("note_markers") or []) if str(x).strip()]
        if block.get("type") == "heading":
            heading_hints[key] = {
                "level": max(1, min(6, int(block.get("level") or 2))),
                "note_markers": [str(x).strip() for x in (block.get("note_markers") or []) if str(x).strip()],
            }
        elif block.get("type") == "quote":
            quote_hints.add(key)

    rebuilt = []
    for paragraph in re.split(r"\n\s*\n+", fallback_text):
        paragraph = cleanup_ocr_text(paragraph)
        if not paragraph:
            continue
        marker_match = re.match(r"^(#{1,6})\s*(.+)$", paragraph)
        if marker_match:
            text = cleanup_ocr_text(marker_match.group(2))
            key = compact_heading_key(text)
            hint = heading_hints.get(key) or {}
            block_type = "heading"
            level = int(hint.get("level") or len(marker_match.group(1)))
            note_markers = hint.get("note_markers") or []
        else:
            text = paragraph
            key = compact_heading_key(text)
            hint = heading_hints.get(key)
            if hint:
                block_type = "heading"
                level = int(hint.get("level") or 2)
                note_markers = hint.get("note_markers") or []
            elif key in quote_hints:
                block_type = "quote"
                level = 0
                note_markers = []
            else:
                block_type = "paragraph"
                level = 0
                note_markers = first_note_markers if not rebuilt and continued_first else []
        rebuilt.append(
            {
                "type": block_type,
                "level": max(1, min(6, level)) if block_type == "heading" else 0,
                "text": text,
                "continued_from_previous": bool(continued_first and not rebuilt and block_type == "paragraph"),
                "note_markers": note_markers,
            }
        )
    return rebuilt


def normalize_blocks(data, fallback_text):
    blocks = []
    for item in data.get("blocks") or []:
        if not isinstance(item, dict):
            continue
        block_type = str(item.get("type") or "paragraph").strip().lower()
        if block_type not in ("heading", "paragraph", "quote"):
            continue
        text = cleanup_ocr_text(item.get("text") or "")
        if not text:
            continue
        try:
            level = int(item.get("level") or 0)
        except Exception:
            level = 0
        if block_type == "heading":
            level = max(1, min(6, level or 2))
        else:
            level = 0
        blocks.append(
            {
                "type": block_type,
                "level": level,
                "text": text,
                "continued_from_previous": normalize_bool(item.get("continued_from_previous")),
                "note_markers": [str(x).strip() for x in (item.get("note_markers") or []) if str(x).strip()],
            }
        )
    if blocks:
        fallback_len = text_coverage_length(fallback_text)
        block_len = sum(text_coverage_length(block.get("text") or "") for block in blocks)
        if fallback_len and block_len < fallback_len * 0.6:
            rebuilt = rebuild_blocks_from_text_with_structural_hints(blocks, fallback_text)
            if rebuilt:
                return rebuilt
        return blocks
    for paragraph in re.split(r"\n\s*\n+", fallback_text):
        paragraph = cleanup_ocr_text(paragraph)
        if paragraph:
            blocks.append(
                {
                    "type": "heading" if paragraph.startswith("#") else "paragraph",
                    "level": min(6, max(1, len(re.match(r"^#+", paragraph).group(0)))) if paragraph.startswith("#") else 0,
                    "text": paragraph.lstrip("#").strip() if paragraph.startswith("#") else paragraph,
                    "continued_from_previous": False,
                    "note_markers": [],
                }
            )
    return blocks


def blocks_to_markdown(blocks):
    parts = []
    for block in blocks:
        text = block.get("text") or ""
        if not text:
            continue
        if block.get("type") == "heading":
            level = max(1, min(6, int(block.get("level") or 2)))
            parts.append("{0} {1}".format("#" * level, text))
        elif block.get("type") == "quote":
            parts.append("> {0}".format(text))
        else:
            parts.append(text)
    return "\n\n".join(parts)


def normalize_notes(data):
    notes = []
    for item in data.get("notes") or []:
        if isinstance(item, str):
            text = cleanup_ocr_text(item)
            if text:
                notes.append({"marker": "", "type": "unknown", "text": text, "anchor": ""})
            continue
        if not isinstance(item, dict):
            continue
        text = cleanup_ocr_text(item.get("text") or "")
        if not text:
            continue
        notes.append(
            {
                "marker": str(item.get("marker") or "").strip(),
                "type": str(item.get("type") or "unknown").strip() or "unknown",
                "text": text,
                "anchor": cleanup_ocr_text(item.get("anchor") or ""),
            }
        )
    return notes


def compact_page_for_repair(result):
    compact_blocks = []
    for index, block in enumerate(result.get("blocks") or []):
        compact_blocks.append(
            {
                "index": index,
                "type": block.get("type") or "paragraph",
                "level": block.get("level") or 0,
                "text": block.get("text") or "",
                "continued_from_previous": bool(block.get("continued_from_previous")),
                "note_markers": block.get("note_markers") or [],
            }
        )
    return {
        "page": int(result.get("page") or 0),
        "title": result.get("title") or "",
        "page_role": result.get("page_role") or "unknown",
        "page_continues_previous": bool(result.get("page_continues_previous")),
        "blocks": compact_blocks,
        "notes": result.get("notes") or [],
    }


def build_semantic_repair_prompt(page_results, risk_summary):
    payload = {
        "risk_summary": risk_summary,
        "pages": [compact_page_for_repair(result) for result in page_results],
    }
    return SEMANTIC_REPAIR_PROMPT + "\n\n已有 OCR 结果与风险说明：\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def semantic_repair_patch(page_results, image_paths, settings, risk_summary, cancel_callback=None):
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
        max_image_side=vision_max_image_side(settings),
    )
    prompt = build_semantic_repair_prompt(page_results, risk_summary)
    result = client.vision_chat(
        prompt,
        image_paths,
        max_tokens=int(settings.get("repair_max_tokens", 1200)),
        temperature=0,
        stream_callback=None,
        cancel_callback=cancel_callback,
    )
    data = parse_json_object(result.get("text") or "")
    if not isinstance(data.get("repairs") or [], list):
        raise RuntimeError("Semantic repair response did not contain repairs list.")
    return {
        "patch": data,
        "response_text": result.get("text") or "",
        "usage": result.get("usage") or {},
        "raw": result.get("raw") or {},
    }


def apply_semantic_repair_patch(page_results_by_page, patch_data):
    changes = []
    for repair in patch_data.get("repairs") or []:
        try:
            page = int(repair.get("page") or 0)
        except Exception:
            continue
        result = page_results_by_page.get(page)
        if not result:
            continue
        before = {
            "title": result.get("title") or "",
            "headings": [block.get("text") for block in result.get("blocks") or [] if block.get("type") == "heading"],
            "page_continues_previous": bool(result.get("page_continues_previous")),
            "notes": len(result.get("notes") or []),
        }
        blocks = list(result.get("blocks") or [])
        delete_indices = []
        for value in repair.get("delete_block_indices") or []:
            try:
                index = int(value)
            except Exception:
                continue
            if 0 <= index < len(blocks):
                block = blocks[index]
                block_text = str(block.get("text") or "").strip()
                page_title = str(result.get("title") or "").strip()
                if (
                    block.get("type") == "heading"
                    and block_text
                    and compact_heading_key(block_text) == compact_heading_key(page_title)
                    and not bool(result.get("page_continues_previous"))
                ):
                    continue
                delete_indices.append(index)
        if delete_indices:
            blocks = [block for index, block in enumerate(blocks) if index not in set(delete_indices)]
        if "set_title" in repair:
            result["title"] = str(repair.get("set_title") or "").strip()
        if "set_page_continues_previous" in repair:
            result["page_continues_previous"] = normalize_bool(repair.get("set_page_continues_previous"))
        for item in repair.get("set_block_continued_from_previous") or []:
            try:
                index = int(item.get("block_index"))
            except Exception:
                continue
            if 0 <= index < len(blocks):
                blocks[index]["continued_from_previous"] = normalize_bool(item.get("value"))
        if isinstance(repair.get("replace_notes"), list):
            result["notes"] = normalize_notes({"notes": repair.get("replace_notes")})
        if isinstance(repair.get("add_notes"), list):
            result["notes"] = list(result.get("notes") or []) + normalize_notes({"notes": repair.get("add_notes")})
        result["blocks"] = blocks
        result["text"] = blocks_to_markdown(blocks)
        after = {
            "title": result.get("title") or "",
            "headings": [block.get("text") for block in result.get("blocks") or [] if block.get("type") == "heading"],
            "page_continues_previous": bool(result.get("page_continues_previous")),
            "notes": len(result.get("notes") or []),
        }
        changes.append({"page": page, "before": before, "after": after, "reason": repair.get("reason") or ""})
    return changes


def collect_toc_candidates(page_results):
    candidates = []
    for result in page_results:
        try:
            page = int(result.get("page") or 0)
        except Exception:
            page = 0
        page_role = str(result.get("page_role") or "unknown")
        title = str(result.get("title") or "").strip()
        for block_index, block in enumerate(result.get("blocks") or []):
            if block.get("type") != "heading":
                continue
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            candidates.append(
                {
                    "page": page,
                    "block_index": block_index,
                    "text": text,
                    "ocr_level": int(block.get("level") or 2),
                    "page_role": page_role,
                    "page_title": title,
                    "page_continues_previous": bool(result.get("page_continues_previous")),
                }
            )
    return candidates


def filtered_toc_candidates(candidates):
    text_counts = {}
    for item in candidates:
        key = compact_heading_key(item.get("text") or "")
        if key:
            text_counts[key] = text_counts.get(key, 0) + 1
    filtered = []
    skip_roles = set(["cover", "blank", "copyright", "back_cover"])
    for item in candidates:
        text = cleanup_ocr_text(item.get("text") or "")
        key = compact_heading_key(text)
        if not text or not key:
            continue
        if item.get("page_role") in skip_roles:
            continue
        if text_counts.get(key, 0) > 3:
            continue
        if len(text) > 80:
            continue
        if re.match(r"^\d+$", text):
            continue
        filtered.append(item)
    return filtered or candidates


def compact_toc_candidates(candidates):
    compact = []
    for item in candidates:
        compact.append(
            {
                "page": int(item.get("page") or 0),
                "block_index": int(item.get("block_index") or 0),
                "text": cleanup_ocr_text(item.get("text") or ""),
                "ocr_level": int(item.get("ocr_level") or 2),
                "page_role": str(item.get("page_role") or "unknown"),
            }
        )
    return compact


def fallback_toc_plan(candidates, reason):
    items = []
    seen = set()
    for item in candidates:
        key = (int(item.get("page") or 0), int(item.get("block_index") or 0))
        if key in seen:
            continue
        seen.add(key)
        label = cleanup_ocr_text(item.get("text") or "")
        if not label:
            continue
        items.append(
            {
                "page": key[0],
                "block_index": key[1],
                "label": label,
                "level": max(1, min(4, int(item.get("ocr_level") or 2))),
                "reason": "Fallback TOC after LLM planning failed",
            }
        )
    return {"items": items, "fallback": True, "fallback_reason": str(reason or "")[:1000]}


def plan_toc_single_request(page_results, settings, cancel_callback=None, retry_callback=None):
    candidates = filtered_toc_candidates(collect_toc_candidates(page_results))
    if not candidates:
        return {"items": []}
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=max(600, int(settings.get("request_timeout", 180))),
        max_image_side=vision_max_image_side(settings),
    )
    prompt = TOC_PROMPT + "\n\n标题候选：\n" + json.dumps(compact_toc_candidates(candidates), ensure_ascii=False, separators=(",", ":"))
    attempt = 1
    while True:
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        try:
            result = client.text_chat(
                prompt,
                max_tokens=int(settings.get("toc_max_tokens", 65536)),
                temperature=0,
                cancel_callback=cancel_callback,
            )
            data = parse_json_object(result.get("text") or "")
            break
        except Exception as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            if attempt >= 3 and retry_callback is None:
                return fallback_toc_plan(candidates, exc)
            if retry_callback is None:
                raise
            decision_page = -2 if attempt >= 3 else -1
            decision = retry_callback(decision_page, attempt, str(exc))
            if decision == "fallback":
                return fallback_toc_plan(candidates, exc)
            if decision == "abandon":
                raise RuntimeError("Abandoned by user")
            attempt += 1
    items = []
    candidate_keys = {(item["page"], item["block_index"]) for item in candidates}
    for item in data.get("items") or []:
        try:
            page = int(item.get("page") or 0)
            block_index = int(item.get("block_index"))
            level = max(1, min(4, int(item.get("level") or 1)))
        except Exception:
            continue
        if (page, block_index) not in candidate_keys:
            continue
        label = cleanup_ocr_text(item.get("label") or "")
        if not label:
            continue
        items.append(
            {
                "page": page,
                "block_index": block_index,
                "label": label,
                "level": level,
                "reason": str(item.get("reason") or ""),
            }
        )
    return {"items": items, "raw": data, "usage": result.get("usage") or {}}


def toc_candidate_chunks(candidates, chunk_size=120):
    chunk_size = max(1, int(chunk_size or 120))
    return [candidates[index : index + chunk_size] for index in range(0, len(candidates), chunk_size)]


def parse_toc_items(data, candidates):
    items = []
    candidate_keys = {(item["page"], item["block_index"]) for item in candidates}
    for item in data.get("items") or []:
        try:
            page = int(item.get("page") or 0)
            block_index = int(item.get("block_index"))
            level = max(1, min(4, int(item.get("level") or 1)))
        except Exception:
            continue
        if (page, block_index) not in candidate_keys:
            continue
        label = cleanup_ocr_text(item.get("label") or "")
        if not label:
            continue
        items.append(
            {
                "page": page,
                "block_index": block_index,
                "label": label,
                "level": level,
                "reason": str(item.get("reason") or ""),
            }
        )
    return items


def iter_complete_json_objects(text):
    text = repair_json_object_text(text)
    stack = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            escaped = False
            continue
        if char == "{":
            stack.append(index)
            continue
        if char == "}":
            if not stack:
                continue
            start = stack.pop()
            yield text[start : index + 1]


def salvage_toc_items_from_text(text, candidates):
    items = []
    for object_text in iter_complete_json_objects(text):
        try:
            data = json.loads(object_text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if "items" in data:
            items.extend(parse_toc_items(data, candidates))
        elif {"page", "block_index", "label"}.issubset(set(data)):
            items.extend(parse_toc_items({"items": [data]}, candidates))
    deduped = []
    seen = set()
    for item in items:
        key = (int(item.get("page") or 0), int(item.get("block_index") or 0))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_toc_json_correction_prompt(candidates, error_message, previous_response):
    return (
        "上一次 EPUB 目录规划返回的内容不是合法 JSON。请根据错误信息修正它，只输出一个严格 JSON 对象，"
        "不要 Markdown 代码块，不要解释，不要新增候选之外的目录项。\n\n"
        "合法输出格式仍然是：\n"
        '{{"items":[{{"page":1,"block_index":0,"label":"标题","level":1,"reason":"简短原因"}}]}}\n\n'
        "可用标题候选：\n{candidates}\n\n"
        "JSON 解析错误：\n{error}\n\n"
        "上一次原始返回：\n{response}"
    ).format(
        candidates=json.dumps(compact_toc_candidates(candidates), ensure_ascii=False, separators=(",", ":")),
        error=str(error_message or "")[:2000],
        response=str(previous_response or "")[:24000],
    )


def plan_toc_chunk(
    client,
    candidates,
    settings,
    chunk_index,
    chunk_count,
    cancel_callback=None,
    retry_callback=None,
    status_callback=None,
):
    prompt = TOC_PROMPT
    if chunk_count > 1:
        prompt += "\n\n这是标题候选分块 {0}/{1}。只从本分块候选中选择目录项；不要补写其他分块的标题。".format(
            chunk_index,
            chunk_count,
        )
    prompt += "\n\n标题候选：\n" + json.dumps(
        compact_toc_candidates(candidates),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    attempt = 1
    correction_attempted = False
    while True:
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        started = time.time()
        if status_callback:
            status_callback(
                "TOC chunk {0}/{1}: planning {2} candidate(s), attempt {3}...".format(
                    chunk_index,
                    chunk_count,
                    len(candidates),
                    attempt,
                )
            )
        try:
            stream_state = {"chars": 0, "next_report": 2000}

            def toc_stream_delta(delta):
                stream_state["chars"] += len(delta or "")
                if status_callback and stream_state["chars"] >= stream_state["next_report"]:
                    status_callback(
                        "TOC chunk {0}/{1}: received {2} streamed character(s)...".format(
                            chunk_index,
                            chunk_count,
                            stream_state["chars"],
                        )
                    )
                    stream_state["next_report"] += 2000

            result = client.text_chat(
                prompt,
                max_tokens=int(settings.get("toc_max_tokens", 65536)),
                temperature=0,
                stream_callback=toc_stream_delta,
                cancel_callback=cancel_callback,
            )
            response_text = result.get("text") or ""
            try:
                data = parse_json_object(response_text)
                items = parse_toc_items(data, candidates)
            except Exception as parse_exc:
                if not correction_attempted:
                    correction_attempted = True
                    prompt = build_toc_json_correction_prompt(candidates, parse_exc, response_text)
                    attempt += 1
                    if status_callback:
                        status_callback(
                            "TOC chunk {0}/{1}: JSON parse failed; retrying once with the error and previous response.".format(
                                chunk_index,
                                chunk_count,
                            )
                        )
                    continue
                items = salvage_toc_items_from_text(response_text, candidates)
                if not items:
                    fallback = fallback_toc_plan(candidates, parse_exc)
                    fallback["fallback_reason"] = "TOC JSON correction failed; using fallback. {0}".format(parse_exc)
                    if status_callback:
                        status_callback(
                            "TOC chunk {0}/{1}: JSON correction failed; using fallback TOC rules.".format(
                                chunk_index,
                                chunk_count,
                            )
                        )
                    return fallback
                data = {"items": items, "_salvaged_from_partial_json": True}
                if status_callback:
                    status_callback(
                        "TOC chunk {0}/{1}: salvaged {2} complete item(s) after correction failed.".format(
                            chunk_index,
                            chunk_count,
                            len(items),
                        )
                    )
            if status_callback:
                status_callback(
                    "TOC chunk {0}/{1}: accepted {2} item(s) in {3:.1f}s.".format(
                        chunk_index,
                        chunk_count,
                        len(items),
                        time.time() - started,
                    )
                )
            return {
                "items": items,
                "raw": data,
                "usage": result.get("usage") or {},
                "fallback": False,
            }
        except Exception as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            if attempt >= 3 and retry_callback is None:
                if status_callback:
                    status_callback(
                        "TOC chunk {0}/{1}: using fallback after {2:.1f}s failure: {3}".format(
                            chunk_index,
                            chunk_count,
                            time.time() - started,
                            str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                        )
                    )
                return fallback_toc_plan(candidates, exc)
            if retry_callback is None:
                raise
            if status_callback:
                status_callback(
                    "TOC chunk {0}/{1}: attempt {2} failed after {3:.1f}s: {4}".format(
                        chunk_index,
                        chunk_count,
                        attempt,
                        time.time() - started,
                        str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                    )
                )
            decision_page = -2 if attempt >= 3 else -1
            decision = retry_callback(decision_page, attempt, str(exc))
            if decision == "fallback":
                return fallback_toc_plan(candidates, exc)
            if decision == "abandon":
                raise RuntimeError("Abandoned by user")
            attempt += 1


def plan_toc(page_results, settings, cancel_callback=None, retry_callback=None, status_callback=None):
    candidates = filtered_toc_candidates(collect_toc_candidates(page_results))
    if not candidates:
        if status_callback:
            status_callback("TOC planning skipped: no heading candidates.")
        return {"items": []}
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=max(3600, int(settings.get("request_timeout", 180))),
        max_image_side=vision_max_image_side(settings),
    )
    chunks = toc_candidate_chunks(candidates, int(settings.get("toc_chunk_size", 60) or 60))
    if status_callback:
        status_callback(
            "TOC planning: {0} candidate(s), {1} chunk(s), chunk size {2}.".format(
                len(candidates),
                len(chunks),
                max(len(chunk) for chunk in chunks) if chunks else 0,
            )
        )
    merged_items = []
    seen = set()
    raw_chunks = []
    fallback_reasons = []
    started = time.time()
    for index, chunk in enumerate(chunks, 1):
        chunk_plan = plan_toc_chunk(
            client,
            chunk,
            settings,
            index,
            len(chunks),
            cancel_callback=cancel_callback,
            retry_callback=retry_callback,
            status_callback=status_callback,
        )
        raw_chunks.append(chunk_plan.get("raw") or {})
        if chunk_plan.get("fallback"):
            fallback_reasons.append(chunk_plan.get("fallback_reason") or "")
        for item in chunk_plan.get("items") or []:
            key = (int(item.get("page") or 0), int(item.get("block_index") or 0))
            if key in seen:
                continue
            seen.add(key)
            merged_items.append(item)
        if status_callback:
            status_callback(
                "TOC planning progress: {0}/{1} chunk(s), {2} merged item(s).".format(
                    index,
                    len(chunks),
                    len(merged_items),
                )
            )
    if status_callback:
        status_callback(
            "TOC planning finished in {0:.1f}s with {1} item(s).".format(
                time.time() - started,
                len(merged_items),
            )
        )
    return {
        "items": merged_items,
        "raw_chunks": raw_chunks,
        "candidate_count": len(candidates),
        "chunk_count": len(chunks),
        "fallback": bool(fallback_reasons),
        "fallback_reason": "\n".join(reason for reason in fallback_reasons if reason)[:1000],
    }


def invalid_json_error(response_text, raw_response=None):
    preview = str(response_text or "").strip()[:1000]
    finish_reason = model_finish_reason(raw_response)
    if finish_reason in ("length", "max_tokens"):
        return InvalidJsonResponseError(
            "Model response was not valid JSON because it was truncated by the page max_tokens budget.\n\n{0}".format(
                preview
            ),
            response_text=response_text,
            raw_response=raw_response,
        )
    return InvalidJsonResponseError(
        "Model response was not valid JSON.\n\n{0}".format(preview),
        response_text=response_text,
        raw_response=raw_response,
    )


def build_page_json_correction_prompt(error_message, previous_response):
    return (
        "上一次单页 OCR 返回的内容不是可用的合法 JSON。请只修正 JSON 格式，不要重新 OCR、不要补写新内容，"
        "不要 Markdown 代码块，不要解释。\n\n"
        "必须返回这个 schema 的一个 JSON 对象：\n"
        "{{"
        '"text":"本页完整可重排正文，标题可用 Markdown # 标出",'
        '"page_continues_previous":false,'
        '"page_role":"cover|front_matter|toc|body|illustration|blank|copyright|unknown",'
        '"title":"",'
        '"blocks":[],'
        '"has_illustrations":false,'
        '"illustrations":[],'
        '"notes":[]'
        "}}\n\n"
        "如果上一次返回里能看到 text 字段，请保留其中的正文。"
        "如果上一次返回里能看到 illustrations，即使 text 为空也要保留图片信息，并修正为 page_role=\"illustration\" 或合适角色、has_illustrations=true。"
        "如果无法恢复正文且页面确实表示为空白，返回 page_role=\"blank\"、text=\"\"、blocks=[]、has_illustrations=false、illustrations=[]。"
        "不要把错误信息写进正文。\n\n"
        "JSON 解析/校验错误：\n{error}\n\n"
        "上一次原始返回：\n{response}"
    ).format(
        error=str(error_message or "")[:2000],
        response=str(previous_response or "")[:24000],
    )


def correct_page_json_response(page_number, image_path, settings, invalid_error, cancel_callback=None):
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
        max_image_side=vision_max_image_side(settings),
    )
    result = client.text_chat(
        build_page_json_correction_prompt(invalid_error, invalid_error.response_text),
        max_tokens=min(page_max_tokens(settings), 8192),
        temperature=0,
        cancel_callback=cancel_callback,
    )
    return normalize_page_result(
        page_number,
        image_path,
        result.get("text") or "",
        usage=result.get("usage") or {},
        seconds=result.get("seconds"),
        settings=settings,
        raw_response=result.get("raw") or {},
    )


def backup_page_result_from_invalid_json(page_number, image_path, response_text, usage=None, seconds=None, raw_response=None):
    text = cleanup_ocr_text(extract_json_string_value(response_text, "text"))
    if not text:
        raise invalid_json_error(response_text, raw_response)
    data = {
        "text": text,
        "page_continues_previous": extract_json_bool_value(response_text, "page_continues_previous", False),
        "page_role": extract_json_string_value(response_text, "page_role") or "unknown",
        "title": extract_json_string_value(response_text, "title"),
        "blocks": [],
        "has_illustrations": False,
        "illustrations": [],
        "notes": [],
        "_backup_from_invalid_json": True,
    }
    blocks = normalize_blocks(data, text)
    return {
        "page": int(page_number),
        "image_path": str(image_path),
        "text": text,
        "blocks": blocks,
        "page_continues_previous": normalize_bool(data.get("page_continues_previous")),
        "page_role": str(data.get("page_role") or "unknown"),
        "title": str(data.get("title") or "").strip(),
        "has_illustrations": False,
        "illustrations": [],
        "notes": [],
        "usage": usage or {},
        "seconds": seconds,
        "raw": {
            "_backup_from_invalid_json": True,
            "response_text": response_text,
            "raw_response": raw_response or {},
        },
    }


def normalize_page_result(page_number, image_path, response_text, usage=None, seconds=None, settings=None, raw_response=None):
    try:
        data = parse_json_object(response_text)
        text = cleanup_ocr_text(data.get("text") or "")
    except Exception:
        raise invalid_json_error(response_text, raw_response)
    if not isinstance(data, dict):
        raise invalid_json_error(response_text, raw_response)
    blocks = normalize_blocks(data, text)
    if not text and blocks:
        text = blocks_to_markdown(blocks)
    illustrations = []
    for item in data.get("illustrations") or []:
        bbox = item.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            bbox = [max(0.0, min(1.0, float(value))) for value in bbox]
        except Exception:
            continue
        illustrations.append(
            {
                "bbox": bbox,
                "caption": str(item.get("caption") or "").strip(),
                "insert_after": str(item.get("insert_after") or "").strip(),
            }
        )
    page_role = str(data.get("page_role") or "unknown")
    if not text.strip() and not illustrations and page_role not in ("blank", "cover", "copyright"):
        raise InvalidJsonResponseError(
            "Model response JSON did not contain usable OCR text or illustration data.",
            response_text=response_text,
            raw_response=raw_response,
        )
    result = {
        "page": int(page_number),
        "image_path": str(image_path),
        "text": text,
        "blocks": blocks,
        "page_continues_previous": normalize_bool(data.get("page_continues_previous")),
        "page_role": page_role,
        "title": str(data.get("title") or "").strip(),
        "has_illustrations": bool(data.get("has_illustrations") or illustrations),
        "illustrations": illustrations,
        "notes": normalize_notes(data),
        "usage": usage or {},
        "seconds": seconds,
        "raw": data,
    }
    return result


def model_finish_reason(raw_response):
    try:
        return str(((raw_response.get("choices") or [{}])[0]).get("finish_reason") or "").strip().lower()
    except Exception:
        return ""


def page_max_tokens(settings):
    configured = int(settings.get("max_tokens_per_page", 65536) or 65536)
    return max(65536, configured)


def ocr_page(page_number, image_path, settings, stream_callback=None, cancel_callback=None):
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
        max_image_side=vision_max_image_side(settings),
    )
    chunks = []

    def on_delta(delta):
        chunks.append(delta)
        if stream_callback:
            stream_callback(page_number, delta)

    started = time.time()
    result = client.vision_chat(
        build_page_prompt(settings),
        [image_path],
        max_tokens=page_max_tokens(settings),
        temperature=0,
        stream_callback=on_delta,
        cancel_callback=cancel_callback,
    )
    text = result.get("text") or "".join(chunks)
    return normalize_page_result(
        page_number,
        image_path,
        text,
        usage=result.get("usage") or {},
        seconds=result.get("seconds") or (time.time() - started),
        settings=settings,
        raw_response=result.get("raw") or {},
    )


def ocr_page_plain_text_recovery(
    page_number,
    image_path,
    settings,
    error_message,
    previous_response,
    stream_callback=None,
    cancel_callback=None,
):
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
        max_image_side=vision_max_image_side(settings),
    )
    chunks = []

    def on_delta(delta):
        chunks.append(delta)
        if stream_callback:
            stream_callback(page_number, delta)

    started = time.time()
    prompt = PLAIN_TEXT_RECOVERY_PROMPT.format(
        error=str(error_message or "")[:2400],
        response=compact_previous_response_for_prompt(previous_response, 3000),
    )
    result = client.vision_chat(
        prompt,
        [image_path],
        max_tokens=min(page_max_tokens(settings), 16384),
        temperature=0,
        stream_callback=on_delta,
        cancel_callback=cancel_callback,
    )
    text = cleanup_ocr_text(result.get("text") or "".join(chunks))
    text = re.sub(r"^```(?:text)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        raise RuntimeError("Plain-text OCR recovery returned no usable text.")
    return {
        "page": int(page_number),
        "image_path": str(image_path),
        "text": text,
        "blocks": [],
        "page_continues_previous": False,
        "page_role": "body",
        "title": "",
        "has_illustrations": False,
        "illustrations": [],
        "notes": [],
        "usage": result.get("usage") or {},
        "seconds": result.get("seconds") or (time.time() - started),
        "raw": {
            "_plain_text_recovery": True,
            "response_text": result.get("text") or "".join(chunks),
            "raw_response": result.get("raw") or {},
        },
    }


def ocr_page_with_retry(
    page_number,
    image_path,
    settings,
    stream_callback=None,
    cancel_callback=None,
    retry_callback=None,
):
    attempt = 1
    previous_invalid_response = None
    correction_failed_responses = set()
    backup_failed_responses = set()
    retry_error_message = ""
    retry_response_text = ""
    stream_repetition_count = 0
    while True:
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        try:
            attempt_settings = dict(settings or {})
            if attempt > 1:
                attempt_settings["_page_retry_error"] = retry_error_message
                attempt_settings["_page_retry_response"] = retry_response_text
            return ocr_page(page_number, image_path, attempt_settings, stream_callback, cancel_callback)
        except InvalidJsonResponseError as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            stream_repetition_count = 0
            response_key = str(exc.response_text or "").strip()
            finish_reason = model_finish_reason(exc.raw_response)
            correction_error = ""
            if (
                response_key
                and response_key not in correction_failed_responses
                and finish_reason not in ("length", "max_tokens")
            ):
                try:
                    return correct_page_json_response(page_number, image_path, settings, exc, cancel_callback)
                except Exception as correction_exc:
                    correction_error = str(correction_exc)
                    correction_failed_responses.add(response_key)
            backup_error = ""
            if (
                response_key
                and previous_invalid_response == response_key
                and response_key not in backup_failed_responses
                and finish_reason not in ("length", "max_tokens")
            ):
                try:
                    return backup_page_result_from_invalid_json(
                        page_number,
                        image_path,
                        exc.response_text,
                        usage={},
                        seconds=None,
                        raw_response=exc.raw_response,
                    )
                except InvalidJsonResponseError as backup_exc:
                    backup_error = str(backup_exc)
                    backup_failed_responses.add(response_key)
            previous_invalid_response = response_key
            retry_error_parts = [str(exc)]
            if correction_error:
                retry_error_parts.append("Automatic JSON correction failed: {0}".format(correction_error))
            if backup_error:
                retry_error_parts.append("Partial-response backup failed: {0}".format(backup_error))
            retry_error_message = "\n".join(retry_error_parts)
            retry_response_text = response_key
            if retry_callback is None:
                raise
            decision = retry_callback(page_number, attempt, str(exc))
            if decision == "abandon":
                raise RuntimeError("Abandoned by user")
            attempt += 1
        except StreamRepetitionError as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            stream_repetition_count += 1
            retry_error_message = str(exc)
            retry_response_text = str(exc.response_text or "").strip()
            if stream_repetition_count >= 2:
                try:
                    return ocr_page_plain_text_recovery(
                        page_number,
                        image_path,
                        settings,
                        retry_error_message,
                        retry_response_text,
                        stream_callback=stream_callback,
                        cancel_callback=cancel_callback,
                    )
                except Exception as recovery_exc:
                    retry_error_message = "{0}\nPlain-text recovery failed: {1}".format(
                        retry_error_message,
                        recovery_exc,
                    )
                    retry_response_text = compact_previous_response_for_prompt(retry_response_text, 3000)
            if retry_callback is None:
                raise
            decision = retry_callback(page_number, attempt, str(exc))
            if decision == "abandon":
                raise RuntimeError("Abandoned by user")
            attempt += 1
        except Exception as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            if is_fatal_vision_preprocess_error(exc):
                raise
            retry_error_message = str(exc)
            retry_response_text = str(getattr(exc, "response_text", "") or "").strip()
            if retry_callback is None:
                raise
            decision = retry_callback(page_number, attempt, str(exc))
            if decision == "abandon":
                raise RuntimeError("Abandoned by user")
            attempt += 1


def ocr_pages(
    image_paths,
    settings,
    page_started=None,
    page_delta=None,
    page_done=None,
    cancel_callback=None,
    retry_callback=None,
):
    page_items = [(index, image_path) for index, image_path in enumerate(image_paths, 1)]
    return ocr_page_items(
        page_items,
        len(image_paths),
        settings,
        page_started=page_started,
        page_delta=page_delta,
        page_done=page_done,
        cancel_callback=cancel_callback,
        retry_callback=retry_callback,
    )


def ocr_page_items(
    page_items,
    total_pages,
    settings,
    page_started=None,
    page_delta=None,
    page_done=None,
    cancel_callback=None,
    retry_callback=None,
):
    max_workers = max(1, int(settings.get("parallel_pages", 2)))
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending = {}
        remaining = iter(page_items)

        def submit_next():
            try:
                index, image_path = next(remaining)
            except StopIteration:
                return False
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            if page_started:
                page_started(index, total_pages)
            future = pool.submit(
                ocr_page_with_retry,
                index,
                image_path,
                settings,
                page_delta,
                cancel_callback,
                retry_callback,
            )
            pending[future] = index
            return True

        for _ in range(max_workers):
            if not submit_next():
                break

        while pending:
            if cancel_callback and cancel_callback():
                for future in pending:
                    future.cancel()
                raise RuntimeError("Canceled by user")
            done, _ = wait(list(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                page = pending.pop(future)
                if cancel_callback and cancel_callback():
                    for active in pending:
                        active.cancel()
                    raise RuntimeError("Canceled by user")
                result = future.result()
                results[page] = result
                if page_done:
                    page_done(result, total_pages)
                if not (cancel_callback and cancel_callback()):
                    submit_next()
    return [results[index] for index in sorted(results)]


def choose_cover_page(
    image_paths,
    settings,
    cancel_callback=None,
    retry_callback=None,
):
    if not image_paths:
        return 1, "No pages"
    if not settings.get("detect_cover", True):
        return 1, "Cover detection disabled"
    count = max(1, min(int(settings.get("cover_scan_pages", 5)), len(image_paths)))
    candidates = image_paths[:count]
    attempt = 1
    multi_image_failure = ""
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
        max_image_side=cover_vision_max_image_side(settings),
    )
    if count > 1 and settings.get("cover_multi_image", False):
        while True:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            try:
                result = client.vision_chat(
                    COVER_MULTI_PROMPT,
                    candidates,
                    max_tokens=300,
                    temperature=0,
                    stream_callback=None,
                    cancel_callback=cancel_callback,
                )
                data = parse_json_object(result.get("text") or "")
                page = int(data.get("cover_page") or 1)
                page = max(1, min(count, page))
                return page, str(data.get("reason") or "")
            except Exception as exc:
                message = str(exc)
                if cancel_callback and cancel_callback():
                    raise RuntimeError("Canceled by user")
                if is_fatal_vision_preprocess_error(message):
                    multi_image_failure = message.splitlines()[0]
                    break
                if "400" not in message and "BadRequestError" not in message:
                    if retry_callback is None:
                        return 1, "Cover detection response was not JSON"
                    decision = retry_callback(0, attempt, message)
                    if decision == "abandon":
                        raise RuntimeError("Abandoned by user")
                    attempt += 1
                break
    best_page = 1
    best_score = -1.0
    best_reason = "No confident cover detected; using first scanned page."
    if multi_image_failure:
        best_reason = "Multi-image cover detection failed; fell back to single-page scoring. {0}".format(
            multi_image_failure
        )
    while True:
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        for index, image_path in enumerate(candidates, 1):
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
            page_attempt = 1
            while True:
                if cancel_callback and cancel_callback():
                    raise RuntimeError("Canceled by user")
                try:
                    result = client.vision_chat(
                        COVER_PROMPT,
                        [image_path],
                        max_tokens=300,
                        temperature=0,
                        stream_callback=None,
                        cancel_callback=cancel_callback,
                    )
                    data = parse_json_object(result.get("text") or "")
                    reason = str(data.get("reason") or "")
                    raw_is_cover = data.get("is_cover", False)
                    if isinstance(raw_is_cover, str):
                        raw_is_cover = raw_is_cover.strip().lower() in ("1", "true", "yes", "y", "on")
                    is_cover = bool(raw_is_cover)
                    quality = data.get("cover_quality")
                    if quality is None:
                        quality = data.get("score")
                    try:
                        score = float(quality)
                    except Exception:
                        score = 1.0 if is_cover else 0.0
                    if is_cover and score > best_score:
                        best_score = score
                        best_page = index
                        best_reason = reason
                    break
                except Exception as exc:
                    if cancel_callback and cancel_callback():
                        raise RuntimeError("Canceled by user")
                    if is_fatal_vision_preprocess_error(exc):
                        raise
                    if retry_callback is None:
                        break
                    decision = retry_callback(0, page_attempt, str(exc))
                    if decision == "abandon":
                        raise RuntimeError("Abandoned by user")
                    page_attempt += 1
        return best_page, best_reason
