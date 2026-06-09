from __future__ import absolute_import, division, print_function, unicode_literals

import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from calibre_plugins.local_pdf_ocr.local_llm import LocalLlmClient


PAGE_PROMPT = """请把这页中文扫描书页转写为可重排电子书文本，并返回严格 JSON。

要求：
1. 只输出页面中真实可见的文字，不要补写下一页或推测缺失内容。
2. 去掉页码、装饰线、页眉、页脚。页眉通常是每页顶部反复出现的书名/章节名/栏目名，例如“阿莱克修斯传”等；除非它也是本页正文中的真实标题，否则不要放入正文。
3. 合并同一自然段内的换行；对话另起段。
4. 使用中文标点。不要改写原文。
5. 不要在中文、数字、英文、标点之间额外添加空格；标题里的装饰性字间距要还原，例如“译 者 序”应输出为“译者序”，“1131 年”应输出为“1131年”。
6. 标题、次级标题、正文段落、引文、角注/脚注必须在 blocks 中标注。text 字段可以是同样内容的 Markdown 预览文本。
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


COMPACT_PAGE_JSON_PROMPT = """\n\n重试输出要求：
上一轮输出不是合法 JSON。请改用更紧凑的 JSON：
1. text 仍然输出本页完整可重排正文。
2. 普通正文段落不要再逐段复制到 blocks；blocks 只保留 heading、quote、带 note_markers 的 paragraph、或 continued_from_previous=true 的首段。
3. 如果没有这些特殊结构，blocks 可以为空数组，后续流程会从 text 自动拆段。
4. notes 和 illustrations 仍按原 schema 输出；没有则为空数组。
5. 只输出一个完整 JSON 对象，不要 Markdown 代码块，不要解释。
"""


def build_page_prompt(settings):
    prompt = PAGE_PROMPT
    if settings and settings.get("convert_traditional_to_simplified", False):
        prompt += SIMPLIFIED_OUTPUT_PROMPT
    if settings and settings.get("_compact_page_json"):
        prompt += COMPACT_PAGE_JSON_PROMPT
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
    last_message = ""
    output_queue = queue.Queue()
    last_rendered = -1

    def read_output():
        if not process.stdout:
            return
        for output_line in process.stdout:
            output_queue.put(output_line)

    reader = threading.Thread(target=read_output)
    reader.daemon = True
    reader.start()
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
        try:
            line = output_queue.get(timeout=0.2)
            last_message = line.strip()
            if progress and last_message:
                progress(max(0, last_rendered), page_count or 1, "Calibre pdftoppm: {0}".format(last_message))
            continue
        except queue.Empty:
            pass
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
        raise RuntimeError("Calibre pdftoppm failed while rendering PDF pages.\n{0}".format(last_message))

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
    last_message = ""
    output_queue = queue.Queue()

    def read_output():
        if not process.stdout:
            return
        for output_line in process.stdout:
            output_queue.put(output_line)

    reader = threading.Thread(target=read_output)
    reader.daemon = True
    reader.start()
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
        try:
            line = output_queue.get(timeout=0.2)
            last_message = line.strip()
            if progress and last_message:
                progress(0, 1, "Calibre PDF renderer: {0}".format(last_message))
            continue
        except queue.Empty:
            pass
        if process.poll() is not None:
            while True:
                try:
                    line = output_queue.get_nowait()
                except queue.Empty:
                    break
                last_message = line.strip()
                if progress and last_message:
                    progress(0, 1, "Calibre PDF renderer: {0}".format(last_message))
            break
    if process.returncode:
        raise RuntimeError("Calibre ebook-convert failed while rendering PDF pages.\n{0}".format(last_message))

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


def repair_json_object_text(text):
    text = str(text or "")
    # Common LLM typo: an object key gains one extra quote, e.g.
    # ,""page_role": "body". Restrict the repair to JSON key positions.
    text = re.sub(r'([{\[,]\s*)""([A-Za-z_][A-Za-z0-9_]*)"\s*:', r'\1"\2":', text)
    return text


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


def compact_heading_key(text):
    return re.sub(r"\s+", "", cleanup_ocr_text(text or ""))


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


def plan_toc(page_results, settings, cancel_callback=None, retry_callback=None):
    candidates = filtered_toc_candidates(collect_toc_candidates(page_results))
    if not candidates:
        return {"items": []}
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=max(600, int(settings.get("request_timeout", 180))),
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


def normalize_page_result(page_number, image_path, response_text, usage=None, seconds=None, settings=None, raw_response=None):
    try:
        data = parse_json_object(response_text)
        text = cleanup_ocr_text(data.get("text") or "")
    except Exception:
        preview = str(response_text or "").strip()[:1000]
        finish_reason = model_finish_reason(raw_response)
        if finish_reason in ("length", "max_tokens"):
            raise RuntimeError(
                "Model response was not valid JSON because it was truncated by the page max_tokens budget.\n\n{0}".format(
                    preview
                )
            )
        raise RuntimeError("Model response was not valid JSON.\n\n{0}".format(preview))
    if not isinstance(data, dict):
        preview = str(response_text or "").strip()[:1000]
        raise RuntimeError("Model response was not valid JSON.\n\n{0}".format(preview))
    blocks = normalize_blocks(data, text)
    if not text and blocks:
        text = blocks_to_markdown(blocks)
    if not text.strip() and str(data.get("page_role") or "") not in ("blank", "cover"):
        raise RuntimeError("Model response JSON did not contain usable OCR text.")
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
    result = {
        "page": int(page_number),
        "image_path": str(image_path),
        "text": text,
        "blocks": blocks,
        "page_continues_previous": normalize_bool(data.get("page_continues_previous")),
        "page_role": str(data.get("page_role") or "unknown"),
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
        return str(((raw_response.get("choices") or [{}])[0]).get("finish_reason") or "")
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


def ocr_page_with_retry(
    page_number,
    image_path,
    settings,
    stream_callback=None,
    cancel_callback=None,
    retry_callback=None,
):
    attempt = 1
    while True:
        if cancel_callback and cancel_callback():
            raise RuntimeError("Canceled by user")
        try:
            attempt_settings = dict(settings or {})
            if attempt > 1:
                attempt_settings["_compact_page_json"] = True
            return ocr_page(page_number, image_path, attempt_settings, stream_callback, cancel_callback)
        except Exception as exc:
            if cancel_callback and cancel_callback():
                raise RuntimeError("Canceled by user")
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
    client = LocalLlmClient(
        settings.get("base_url"),
        model=settings.get("model"),
        timeout=int(settings.get("request_timeout", 180)),
    )
    if count > 1:
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
                    if retry_callback is None:
                        break
                    decision = retry_callback(0, page_attempt, str(exc))
                    if decision == "abandon":
                        raise RuntimeError("Abandoned by user")
                    page_attempt += 1
        return best_page, best_reason
