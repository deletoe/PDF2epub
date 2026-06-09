from __future__ import absolute_import, division, print_function, unicode_literals

import html
import mimetypes
import re
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


try:
    from PIL import Image
except Exception:  # pragma: no cover - depends on Calibre runtime.
    Image = None


def safe_name(value, fallback="book"):
    value = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def split_paragraphs(text):
    parts = []
    for block in re.split(r"\n\s*\n+", str(text or "").strip()):
        block = re.sub(r"[ \t]*\n[ \t]*", "", block.strip())
        if block:
            parts.append(block)
    return parts


def looks_like_heading(text):
    text = str(text or "").strip()
    if not text:
        return False
    if len(text) > 32:
        return False
    return bool(re.match(r"^(第[一二三四五六七八九十百千万零〇\d]+[章节回卷部篇]|[一二三四五六七八九十百千万零〇\d]+[、.．])", text))


def html_paragraph(text):
    text = str(text or "").strip()
    if looks_like_heading(text):
        return "<h2>{0}</h2>".format(html.escape(text))
    cls = ' class="dialogue"' if text.startswith("“") else ""
    return "<p{0}>{1}</p>".format(cls, html.escape(text))


def slug_id(prefix, index):
    return "{0}-{1:04d}".format(prefix, int(index))


def paragraph_class(text, block_type="paragraph"):
    classes = []
    if block_type == "quote":
        classes.append("quote")
    if str(text or "").strip().startswith("“"):
        classes.append("dialogue")
    return ' class="{0}"'.format(" ".join(classes)) if classes else ""


def compact_text_key(text):
    return re.sub(r"\s+", "", str(text or "").strip())


def is_planned_heading(planned_heading_keys, result, block_index):
    if planned_heading_keys is None:
        return False
    try:
        return (int(result.get("page") or 0), int(block_index)) in planned_heading_keys
    except Exception:
        return False


def planned_heading_level(planned_heading_keys, result, block_index):
    if planned_heading_keys is None:
        return None
    try:
        item = planned_heading_keys.get((int(result.get("page") or 0), int(block_index)))
        if item is None:
            return None
        return int(item.get("level") or 1)
    except Exception:
        return None


def is_running_header_block(block, block_index, result, page_continues_previous, planned_heading_keys=None):
    if block_index != 0:
        return False
    if planned_heading_keys is None and not page_continues_previous:
        return False
    if block.get("type") != "heading":
        return False
    if is_planned_heading(planned_heading_keys, result, block_index):
        return False
    block_text = compact_text_key(block.get("text") or "")
    title_text = compact_text_key(result.get("title") or "")
    if title_text and block_text == title_text:
        return True
    if block_text in ("阿莱克修斯传", "译者序", "作者序"):
        return True
    return bool(re.match(r"^第[一二三四五六七八九十百千万零〇\d]+章$", block_text))


def should_skip_heading_block(block, block_index, result, page_continues_previous, illustration_captions, planned_heading_keys):
    if block.get("type") != "heading":
        return False
    if is_planned_heading(planned_heading_keys, result, block_index):
        return False
    if planned_heading_keys is not None:
        return True
    block_text = compact_text_key(block.get("text") or "")
    if block_text and block_text in illustration_captions:
        return True
    return is_running_header_block(block, block_index, result, page_continues_previous, planned_heading_keys)


SUPERSCRIPT_DIGITS = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
}

CIRCLED_DIGITS = {
    "1": "①",
    "2": "②",
    "3": "③",
    "4": "④",
    "5": "⑤",
    "6": "⑥",
    "7": "⑦",
    "8": "⑧",
    "9": "⑨",
    "10": "⑩",
}

SUPERSCRIPT_TO_DIGIT = {value: key for key, value in SUPERSCRIPT_DIGITS.items()}
CIRCLED_TO_DIGIT = {value: key for key, value in CIRCLED_DIGITS.items()}


def marker_variants(label):
    label = str(label or "").strip()
    if not label:
        return []
    variants = []
    if all(ch in SUPERSCRIPT_DIGITS for ch in label):
        variants.append("".join(SUPERSCRIPT_DIGITS[ch] for ch in label))
    if label in CIRCLED_DIGITS:
        variants.append(CIRCLED_DIGITS[label])
    variants.extend(("[{0}]".format(label), "〔{0}〕".format(label), "［{0}］".format(label)))
    variants.append(label)
    deduped = []
    for value in variants:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def normalize_note_item(note):
    if isinstance(note, dict):
        return note
    text = str(note or "").strip()
    if not text:
        return {"marker": "", "text": "", "type": "footnote"}
    marker = ""
    match = re.match(r"^(\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+|[①②③④⑤⑥⑦⑧⑨⑩])\s*(.*)$", text)
    if match:
        raw_marker = match.group(1)
        text = match.group(2).strip()
        if all(ch in SUPERSCRIPT_TO_DIGIT for ch in raw_marker):
            marker = "".join(SUPERSCRIPT_TO_DIGIT[ch] for ch in raw_marker)
        elif raw_marker in CIRCLED_TO_DIGIT:
            marker = CIRCLED_TO_DIGIT[raw_marker]
        else:
            marker = raw_marker
    return {"marker": marker, "text": text, "type": "footnote"}


def escape_with_note_refs(text, note_refs):
    escaped = html.escape(str(text or ""))
    for note in note_refs or []:
        ref_html = note_ref_html(note)
        replaced = False
        variants = marker_variants(note.get("label"))
        label_text = str(note.get("label") or "").strip()
        for marker in variants:
            if marker == label_text and marker.isascii() and marker.isdigit():
                continue
            escaped_marker = html.escape(marker)
            if escaped_marker in escaped:
                escaped = escaped.replace(escaped_marker, ref_html, 1)
                replaced = True
                break
        if replaced:
            continue
        anchor = html.escape(note.get("anchor") or "")
        if anchor and anchor in escaped:
            escaped = escaped.replace(anchor, anchor + ref_html, 1)
        else:
            escaped += ref_html
    return escaped


def note_ref_html(note):
    note_text = html.escape(str((note.get("note") or {}).get("text") or ""), quote=True)
    return (
        '<a epub:type="noteref" class="noteref" id="{ref_id}" href="#{note_id}" '
        'data-label="{label}" aria-label="注{label}"></a>'
        '<span class="reader_footer_note js_readerFooterNote" data-wr-footernote="{note_text}"></span>'
    ).format(
        ref_id=html.escape(note["ref_id"]),
        note_id=html.escape(note["note_id"]),
        label=html.escape(note["label"]),
        note_text=note_text,
    )


def block_to_html(block, block_id=None, note_refs=None):
    text = str(block.get("text") or "").strip()
    if not text:
        return ""
    block_type = block.get("type") or "paragraph"
    id_attr = ' id="{0}"'.format(html.escape(block_id)) if block_id else ""
    if block_type == "heading":
        level = int(block.get("level") or 2)
        level = max(2, min(4, level + 1))
        cls = ' class="section-heading"'
        return "<h{level}{id_attr}{cls}>{text}</h{level}>".format(
            level=level, id_attr=id_attr, cls=cls, text=html.escape(text)
        )
    if block_type == "quote":
        return "<p{id_attr}{cls}>{text}</p>".format(
            id_attr=id_attr, cls=paragraph_class(text, "quote"), text=escape_with_note_refs(text, note_refs)
        )
    return "<p{id_attr}{cls}>{text}</p>".format(
        id_attr=id_attr, cls=paragraph_class(text), text=escape_with_note_refs(text, note_refs)
    )


def append_to_previous_paragraph(fragments, text):
    if not fragments:
        return False
    escaped = html.escape(str(text or "").strip())
    if not escaped:
        return True
    last = fragments[-1]
    match = re.match(r"^(<p(?: [^>]*)?>)(.*)(</p>)$", last, re.S)
    if not match:
        return False
    fragments[-1] = "{0}{1}{2}{3}".format(match.group(1), match.group(2), escaped, match.group(3))
    return True


def append_raw_to_previous_paragraph(fragments, raw_html):
    if not fragments or not raw_html:
        return False
    last = fragments[-1]
    match = re.match(r"^(<p(?: [^>]*)?>)(.*)(</p>)$", last, re.S)
    if not match:
        return False
    fragments[-1] = "{0}{1}{2}{3}".format(match.group(1), match.group(2), raw_html, match.group(3))
    return True


def fallback_blocks_from_text(text):
    blocks = []
    for paragraph in split_paragraphs(text):
        if looks_like_heading(paragraph):
            blocks.append({"type": "heading", "level": 2, "text": paragraph, "continued_from_previous": False})
        else:
            blocks.append({"type": "paragraph", "level": 0, "text": paragraph, "continued_from_previous": False})
    return blocks


def note_html(note, index, note_id=None, ref_id=None):
    marker = str(note.get("marker") or index).strip()
    label = marker or str(index)
    note_type = str(note.get("type") or "unknown").strip()
    text = str(note.get("text") or "").strip()
    if not text:
        return ""
    note_id = note_id or slug_id("fn", index)
    ref_id = ref_id or slug_id("fnref", index)
    return (
        '<aside epub:type="footnote" class="note {cls}" id="{note_id}">'
        '<p>{text}</p></aside>'
    ).format(
        cls=html.escape(note_type),
        note_id=html.escape(note_id),
        text=html.escape(text),
    )


def copy_cover_image(source, dest):
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if Image is not None:
        image = Image.open(str(source)).convert("RGB")
        image.thumbnail((1600, 2400))
        image.save(str(dest), quality=90)
    else:
        shutil.copyfile(str(source), str(dest))


def crop_illustration(source, bbox, dest):
    if Image is None:
        return False
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(str(source)).convert("RGB")
    width, height = image.size
    x1, y1, x2, y2 = bbox
    left = int(max(0, min(width - 1, x1 * width)))
    top = int(max(0, min(height - 1, y1 * height)))
    right = int(max(left + 1, min(width, x2 * width)))
    bottom = int(max(top + 1, min(height, y2 * height)))
    cropped = image.crop((left, top, right, bottom))
    cropped.thumbnail((1400, 1800))
    cropped.save(str(dest), quality=88)
    return True


def xhtml_document(title, body):
    return """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../styles/style.css"/>
</head>
<body>
{body}
</body>
</html>
""".format(title=html.escape(title), body=body)


def build_body_pages(page_results, images_dir, planned_heading_keys=None):
    fragments = []
    image_items = []
    toc_entries = []
    illustration_index = 0
    note_index = 0
    heading_index = 0
    block_index_global = 0
    for result in page_results:
        role = result.get("page_role")
        text = result.get("text") or ""
        if role == "blank" and not text:
            continue
        blocks = list(result.get("blocks") or fallback_blocks_from_text(text))
        illustration_captions = set()
        for illustration in result.get("illustrations") or []:
            caption_key = compact_text_key(illustration.get("caption") or "")
            if caption_key:
                illustration_captions.add(caption_key)
        illustration_html = []
        for illustration in result.get("illustrations") or []:
            illustration_index += 1
            image_name = "illustration_{0:04d}.jpg".format(illustration_index)
            image_path = images_dir / image_name
            if not crop_illustration(result.get("image_path"), illustration.get("bbox"), image_path):
                continue
            caption = illustration.get("caption") or ""
            figure = '<figure><img src="../images/{0}" alt="{1}"/>'.format(
                html.escape(image_name), html.escape(caption or "illustration")
            )
            if caption:
                figure += "<figcaption>{0}</figcaption>".format(html.escape(caption))
            figure += "</figure>"
            illustration_html.append((illustration.get("insert_after") or "", figure))
            image_items.append(image_name)

        pending_figures = list(illustration_html)
        page_continues_previous = bool(result.get("page_continues_previous"))
        explicit_note_markers = set()
        for block in blocks:
            for value in block.get("note_markers") or []:
                marker = str(value).strip()
                if marker:
                    explicit_note_markers.add(marker)
        page_notes = []
        for raw_note in result.get("notes") or []:
            note = normalize_note_item(raw_note)
            marker = str(note.get("marker") or "").strip()
            page_notes.append({"note": note, "marker": marker, "used": False})
        for block_index, block in enumerate(blocks):
            if should_skip_heading_block(
                block,
                block_index,
                result,
                page_continues_previous,
                illustration_captions,
                planned_heading_keys,
            ):
                continue
            block_text = str(block.get("text") or "").strip()
            if not block_text:
                continue
            block_index_global += 1
            note_refs = []
            markers = [str(value).strip() for value in (block.get("note_markers") or []) if str(value).strip()]
            for item in page_notes:
                note = item["note"]
                marker = item["marker"]
                anchor = str(note.get("anchor") or "").strip()
                if item["used"]:
                    continue
                if marker and marker in explicit_note_markers:
                    matched_note = marker in markers
                else:
                    matched_note = (marker and marker in markers) or (anchor and anchor in block_text)
                if matched_note:
                    note_index += 1
                    note_id = slug_id("fn", note_index)
                    ref_id = slug_id("fnref", note_index)
                    label = marker or str(note_index)
                    note_refs.append(
                        {"note": note, "note_id": note_id, "ref_id": ref_id, "label": label, "anchor": anchor}
                    )
                    item["used"] = True
            continues_previous = (
                block_index == 0
                and block.get("type") == "paragraph"
                and (page_continues_previous or bool(block.get("continued_from_previous")))
            )
            if continues_previous and append_raw_to_previous_paragraph(
                fragments, escape_with_note_refs(block_text, note_refs)
            ):
                rendered = ""
            else:
                block_id = None
                if block.get("type") == "heading":
                    heading_index += 1
                    block_id = slug_id("heading", heading_index)
                    planned_level = planned_heading_level(planned_heading_keys, result, block_index)
                    if planned_level is not None:
                        block = dict(block)
                        block["level"] = planned_level
                    toc_entries.append(
                        {
                            "id": block_id,
                            "text": block_text,
                            "level": int(block.get("level") or 2),
                            "page": int(result.get("page") or 0),
                            "block_index": block_index,
                        }
                    )
                else:
                    block_id = slug_id("block", block_index_global)
                rendered = block_to_html(block, block_id=block_id, note_refs=note_refs)
                if rendered:
                    fragments.append(rendered)
            for note_ref in note_refs:
                fragments.append(
                    note_html(
                        note_ref["note"],
                        note_index,
                        note_id=note_ref["note_id"],
                        ref_id=note_ref["ref_id"],
                    )
                )
            remaining = []
            for anchor, figure in pending_figures:
                if not anchor or anchor in block_text:
                    fragments.append(figure)
                else:
                    remaining.append((anchor, figure))
            pending_figures = remaining
        for _, figure in pending_figures:
            fragments.append(figure)
        for item in page_notes:
            if item["used"]:
                continue
            note = item["note"]
            note_index += 1
            note_id = slug_id("fn", note_index)
            ref_id = slug_id("fnref", note_index)
            label = str(note.get("marker") or note_index).strip() or str(note_index)
            note_ref_markup = note_ref_html({"note": note, "note_id": note_id, "ref_id": ref_id, "label": label})
            if not append_raw_to_previous_paragraph(fragments, note_ref_markup):
                fragments.append('<p class="note-ref-line">{0}</p>'.format(note_ref_markup))
            rendered_note = note_html(note, note_index, note_id=note_id, ref_id=ref_id)
            if rendered_note:
                anchor = note.get("anchor") or ""
                if anchor:
                    inserted = False
                    for idx in range(len(fragments) - 1, -1, -1):
                        if anchor in fragments[idx]:
                            fragments.insert(idx + 1, rendered_note)
                            inserted = True
                            break
                    if inserted:
                        continue
                fragments.append(rendered_note)
    return "\n".join(fragments), image_items, toc_entries


def build_nav_list(entries):
    if not entries:
        return ""
    items = []
    stack = [{"level": 0, "items": items}]
    for entry in entries:
        level = max(1, min(6, int(entry.get("level") or 2)))
        node = {
            "text": entry.get("text") or "",
            "href": entry.get("href") or "",
            "children": [],
        }
        while stack and level <= stack[-1]["level"]:
            stack.pop()
        stack[-1]["items"].append(node)
        stack.append({"level": level, "items": node["children"]})

    def render(nodes):
        parts = ["<ol>"]
        for node in nodes:
            parts.append('<li><a href="{0}">{1}</a>'.format(html.escape(node["href"]), html.escape(node["text"])))
            if node["children"]:
                parts.append(render(node["children"]))
            parts.append("</li>")
        parts.append("</ol>")
        return "\n".join(parts)

    return render(items)


def apply_toc_plan(toc_entries, toc_plan):
    def int_value(value, default=0):
        if value is None:
            return default
        try:
            return int(value)
        except Exception:
            return default

    if not toc_plan:
        return [
            entry
            for entry in toc_entries
            if entry.get("text")
            and not re.search(r"^(封面|阿莱克修斯传|图书在版编目|CIP|.*小样.*)$", entry.get("text"), re.I)
            and int_value(entry.get("page"), 0) > 1
        ]
    by_key = {(int_value(entry.get("page"), 0), int_value(entry.get("block_index"), -1)): entry for entry in toc_entries}
    planned = []
    for item in toc_plan.get("items") or []:
        try:
            key = (int_value(item.get("page"), 0), int_value(item.get("block_index"), -1))
        except Exception:
            continue
        entry = by_key.get(key)
        if not entry:
            continue
        merged = dict(entry)
        merged["text"] = item.get("label") or entry.get("text") or ""
        merged["level"] = max(1, min(4, int(item.get("level") or entry.get("level") or 1)))
        planned.append(merged)
    return planned


def nav_items(toc_entries):
    entries = []
    for entry in toc_entries:
        level = int(entry.get("level") or 2)
        if level > 4:
            continue
        entries.append({"id": entry["id"], "text": entry["text"], "level": max(1, level)})
    fixed = []
    for entry in entries:
        if "href" not in entry:
            entry = dict(entry)
            entry["href"] = "text/body.xhtml#{0}".format(entry["id"])
        fixed.append(entry)

    return build_nav_list(fixed)


def ncx_document(title, toc_entries):
    nav_entries = []
    for entry in toc_entries:
        level = int(entry.get("level") or 2)
        if level <= 4:
            nav_entries.append(
                {
                    "text": entry.get("text") or "",
                    "level": max(1, level),
                    "src": "text/body.xhtml#{0}".format(entry.get("id") or ""),
                }
            )

    play_order = [0]

    def render_entries(entries):
        parts = []
        stack = [{"level": 0, "children": []}]
        for entry in entries:
            level = max(1, min(6, int(entry.get("level") or 1)))
            node = {"entry": entry, "children": []}
            while stack and level <= stack[-1]["level"]:
                stack.pop()
            stack[-1]["children"].append(node)
            stack.append({"level": level, "children": node["children"]})

        def render_nodes(nodes):
            rendered = []
            for node in nodes:
                play_order[0] += 1
                nav_id = "num_{0}".format(play_order[0])
                entry = node["entry"]
                rendered.append(
                    '<navPoint id="{id}" playOrder="{order}"><navLabel><text>{text}</text></navLabel><content src="{src}"/>'.format(
                        id=html.escape(nav_id),
                        order=play_order[0],
                        text=html.escape(entry.get("text") or ""),
                        src=html.escape(entry.get("src") or ""),
                    )
                )
                if node["children"]:
                    rendered.append(render_nodes(node["children"]))
                rendered.append("</navPoint>")
            return "\n".join(rendered)

        return render_nodes(stack[0]["children"])

    depth = max([int(entry.get("level") or 1) for entry in nav_entries] or [1])
    return """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh-CN">
  <head>
    <meta name="dtb:uid" content="urn:uuid:local-pdf-ocr"/>
    <meta name="dtb:depth" content="{depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{title}</text></docTitle>
  <navMap>
{navmap}
  </navMap>
</ncx>
""".format(depth=depth, title=html.escape(title), navmap=render_entries(nav_entries))


def planned_heading_keys_from_toc(toc_plan):
    if not toc_plan:
        return None
    keys = {}
    for item in toc_plan.get("items") or []:
        try:
            keys[(int(item.get("page") or 0), int(item.get("block_index")))] = item
        except Exception:
            continue
    return keys


def write_epub(output_path, title, authors, page_results, cover_image_path=None, cover_page=None, toc_plan=None):
    output_path = Path(output_path)
    work_dir = output_path.parent / (output_path.stem + "_epub_build")
    if work_dir.exists():
        shutil.rmtree(str(work_dir))
    (work_dir / "META-INF").mkdir(parents=True)
    (work_dir / "OEBPS" / "text").mkdir(parents=True)
    (work_dir / "OEBPS" / "styles").mkdir(parents=True)
    (work_dir / "OEBPS" / "images").mkdir(parents=True)

    (work_dir / "mimetype").write_text("application/epub+zip", encoding="ascii")
    (work_dir / "META-INF" / "container.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
        encoding="utf-8",
    )
    css = """body { font-family: "Songti SC", "Noto Serif CJK SC", serif; line-height: 1.85; margin: 0; padding: 1.2em; color: #202020; }
h1, h2, h3, h4 { text-align: center; font-weight: 500; line-height: 1.5; margin: 2em 0 1.2em; }
h1 { font-size: 1.45em; }
h2 { font-size: 1.2em; }
h3 { font-size: 1.08em; }
h4 { font-size: 1em; }
h2.section-heading { break-before: page; page-break-before: always; }
h1 + h2.section-heading, .cover + h2.section-heading { break-before: auto; page-break-before: auto; }
p { text-indent: 2em; margin: 0.35em 0; text-align: justify; }
p.dialogue { text-indent: 2em; }
p.quote { text-indent: 0; margin: .7em 2em; }
p.note-ref-line { text-indent: 0; margin: 0; line-height: 1; }
a.noteref { text-decoration: none; vertical-align: super; font-size: .75em; line-height: 0; }
a.noteref::after { content: attr(data-label); }
a.noteback { text-decoration: none; color: inherit; }
figure { margin: 1.2em 0; text-align: center; }
figure img { max-width: 100%; height: auto; }
figcaption { color: #666; font-size: .9em; margin-top: .4em; }
aside.note { margin: .6em 0 .9em 2em; color: #555; font-size: .9em; line-height: 1.65; }
aside.note sup { margin-right: .35em; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-width: 100%; height: auto; }
.toc-level-3 { margin-left: 1.2em; }
"""
    (work_dir / "OEBPS" / "styles" / "style.css").write_text(css, encoding="utf-8")

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="styles/style.css" media-type="text/css"/>',
        '<item id="body" href="text/body.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine_items = ['<itemref idref="body"/>']

    if cover_image_path:
        copy_cover_image(cover_image_path, work_dir / "OEBPS" / "images" / "cover.jpg")
        cover_body = '<div class="cover"><img src="../images/cover.jpg" alt="cover"/></div>'
        (work_dir / "OEBPS" / "text" / "cover.xhtml").write_text(xhtml_document("封面", cover_body), encoding="utf-8")
        manifest_items.append('<item id="cover-page" href="text/cover.xhtml" media-type="application/xhtml+xml"/>')
        manifest_items.append('<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        spine_items.insert(0, '<itemref idref="cover-page"/>')

    body, image_items, toc_entries = build_body_pages(
        page_results,
        work_dir / "OEBPS" / "images",
        planned_heading_keys=planned_heading_keys_from_toc(toc_plan),
    )
    toc_entries = apply_toc_plan(toc_entries, toc_plan)
    if not body.strip():
        body = "<p>OCR produced no text.</p>"
    (work_dir / "OEBPS" / "text" / "body.xhtml").write_text(
        xhtml_document(title, '<h1 id="book-title">{0}</h1>\n{1}'.format(html.escape(title), body)),
        encoding="utf-8",
    )
    for index, image_name in enumerate(image_items, 1):
        mime = mimetypes.guess_type(image_name)[0] or "image/jpeg"
        manifest_items.append(
            '<item id="img{0}" href="images/{1}" media-type="{2}"/>'.format(index, html.escape(image_name), mime)
        )

    nav = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head><title>目录</title><link rel="stylesheet" type="text/css" href="styles/style.css"/></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1>{items}</nav></body></html>""".format(
        items=nav_items(toc_entries)
    )
    (work_dir / "OEBPS" / "nav.xhtml").write_text(nav, encoding="utf-8")
    (work_dir / "OEBPS" / "toc.ncx").write_text(ncx_document(title, toc_entries), encoding="utf-8")

    author_xml = "\n".join("<dc:creator>{0}</dc:creator>".format(html.escape(a)) for a in (authors or []))
    metadata_cover = '<meta name="cover" content="cover-image"/>' if cover_image_path else ""
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:local-pdf-ocr</dc:identifier>
    <dc:title>{title}</dc:title>
    {authors}
    <dc:language>zh-CN</dc:language>
    <meta property="dcterms:modified">2026-06-04T00:00:00Z</meta>
    {metadata_cover}
  </metadata>
  <manifest>
    {manifest}
  </manifest>
  <spine toc="ncx">
    {spine}
  </spine>
</package>""".format(
        title=html.escape(title),
        authors=author_xml,
        metadata_cover=metadata_cover,
        manifest="\n    ".join(manifest_items),
        spine="\n    ".join(spine_items),
    )
    (work_dir / "OEBPS" / "content.opf").write_text(opf, encoding="utf-8")

    if output_path.exists():
        output_path.unlink()
    with ZipFile(str(output_path), "w") as zf:
        zf.write(str(work_dir / "mimetype"), "mimetype", compress_type=ZIP_STORED)
        for path in sorted(work_dir.rglob("*")):
            if path.is_file() and path.name != "mimetype":
                zf.write(str(path), path.relative_to(work_dir).as_posix(), compress_type=ZIP_DEFLATED)
    shutil.rmtree(str(work_dir), ignore_errors=True)
    return output_path
