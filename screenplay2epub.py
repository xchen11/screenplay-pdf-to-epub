#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import re
import sys
import textwrap
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
TEXT_OP_RE = re.compile(
    rb"BT\s+"
    rb"(?:-?\d*\.?\d+\s+Tc\s+)?"
    rb"([0-9.]+)\s+0\s+0\s+-?([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+Tm\s+"
    rb"/([A-Za-z0-9]+)\s+1\s+Tf\s+"
    rb"(\[(?:.|\n)*?\]\s+TJ|\((?:\\.|[^\\)])*\)\s+Tj)",
    re.S,
)
TJ_STRING_RE = re.compile(rb"\((?:\\.|[^\\)])*\)")
OCTAL_ESCAPE_RE = re.compile(rb"\\([0-7]{1,3})")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class TextFragment:
    x: float
    y: float
    font: str
    text: str
    page: int


@dataclass
class Line:
    page: int
    x: float
    y: float
    font: str
    text: str
    kind: str = "unknown"


@dataclass
class Block:
    kind: str
    lines: List[Line] = field(default_factory=list)
    speaker: Optional[str] = None
    parenthetical: Optional[str] = None


@dataclass
class TocEntry:
    href: str
    label: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a text-based screenplay PDF into a readable EPUB."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the screenplay PDF")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Path to the output EPUB. Defaults to the PDF name with .epub",
    )
    parser.add_argument("--title", help="Override EPUB title")
    parser.add_argument("--author", help="Override EPUB author")
    parser.add_argument("--cover", type=Path, help="Path to a custom EPUB cover image")
    parser.add_argument("--scene-max-x", type=float, default=130.0)
    parser.add_argument("--dialogue-min-x", type=float, default=165.0)
    parser.add_argument("--parenthetical-min-x", type=float, default=200.0)
    parser.add_argument("--character-min-x", type=float, default=235.0)
    parser.add_argument("--transition-min-x", type=float, default=430.0)
    parser.add_argument("--header-y-min", type=float, default=735.0)
    parser.add_argument("--footer-y-max", type=float, default=95.0)
    parser.add_argument("--line-merge-gap", type=float, default=8.0)
    parser.add_argument("--debug-lines", action="store_true")
    return parser.parse_args()


def detect_cover_format(cover_path: Path) -> tuple[str, str]:
    data = cover_path.read_bytes()
    stripped = data.lstrip()
    if stripped.startswith(b"<?xml"):
        xml_end = stripped.find(b"?>")
        if xml_end != -1:
            stripped = stripped[xml_end + 2 :].lstrip()
    if stripped.startswith(b"<svg"):
        return ("image/svg+xml", ".svg")
    if data.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg", ".jpg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("image/png", ".png")
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ("image/gif", ".gif")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ("image/webp", ".webp")
    raise ValueError(
        "Unsupported cover image format. Use JPG, PNG, GIF, WEBP, or SVG."
    )


def decode_pdf_string(data: bytes) -> str:
    data = OCTAL_ESCAPE_RE.sub(lambda m: bytes([int(m.group(1), 8)]), data)
    data = (
        data.replace(rb"\(", b"(")
        .replace(rb"\)", b")")
        .replace(rb"\n", b"\n")
        .replace(rb"\r", b"\r")
        .replace(rb"\t", b"\t")
        .replace(rb"\b", b"\b")
        .replace(rb"\f", b"\f")
        .replace(rb"\\", b"\\")
    )
    if data.startswith(b"\xFE\xFF"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xFF\xFE"):
        return data[2:].decode("utf-16-le", errors="replace")

    cp1252_text = data.decode("cp1252", errors="replace")
    mac_text = data.decode("mac_roman", errors="replace")

    def score(text: str) -> tuple[int, int]:
        suspicious = sum(text.count(ch) for ch in ("Õ", "Ò", "Ó", "Ô", "�"))
        smart_punct = sum(text.count(ch) for ch in ("’", "‘", "“", "”", "…", "—", "–"))
        return (suspicious, -smart_punct)

    return min((cp1252_text, mac_text), key=score)


def uppercase_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    uppers = sum(1 for ch in letters if ch.isupper())
    return uppers / len(letters)


def is_parenthetical_text(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("(") and stripped.endswith(")")


def is_scene_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    prefixes = (
        "INT.",
        "EXT.",
        "INT./EXT.",
        "INT/EXT.",
        "I/E.",
        "EST.",
        "INT ",
        "EXT ",
    )
    if stripped.startswith(prefixes):
        return True
    return uppercase_ratio(stripped) > 0.9 and " - " in stripped


def is_transition(text: str) -> bool:
    stripped = text.strip()
    return stripped.endswith(":") and uppercase_ratio(stripped) > 0.9


def is_character_cue(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 40:
        return False
    if stripped in {"CUT TO:", "FADE OUT:", "FADE IN:"}:
        return False
    if is_parenthetical_text(stripped):
        return False
    return uppercase_ratio(stripped) > 0.9


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def extract_fragments(pdf_path: Path) -> List[TextFragment]:
    data = pdf_path.read_bytes()
    fragments: List[TextFragment] = []
    page = 0

    for match in STREAM_RE.finditer(data):
        compressed = match.group(1)
        try:
            stream = zlib.decompress(compressed)
        except zlib.error:
            continue
        if b"BT" not in stream or b"/TT" not in stream:
            continue
        if b" Tf " not in stream and b" Tj" not in stream and b" TJ" not in stream:
            continue
        stream_fragments: List[TextFragment] = []
        for _, _, x, y, font, payload in TEXT_OP_RE.findall(stream):
            x_val = float(x.decode("ascii"))
            y_val = float(y.decode("ascii"))
            font_name = font.decode("ascii", errors="replace")
            if payload.startswith(b"["):
                text = "".join(
                    decode_pdf_string(s[1:-1]) for s in TJ_STRING_RE.findall(payload)
                )
            else:
                text = decode_pdf_string(payload[1:-4])
            if text:
                stream_fragments.append(
                    TextFragment(x=x_val, y=y_val, font=font_name, text=text, page=0)
                )
        if stream_fragments:
            page += 1
            for fragment in stream_fragments:
                fragment.page = page
            fragments.extend(stream_fragments)
    return fragments


def group_lines(fragments: Iterable[TextFragment], line_merge_gap: float) -> List[Line]:
    page_buckets: dict[int, dict[float, List[TextFragment]]] = {}
    for fragment in fragments:
        y_key = round(fragment.y, 1)
        page_buckets.setdefault(fragment.page, {}).setdefault(y_key, []).append(fragment)

    lines: List[Line] = []
    for page in sorted(page_buckets):
        for y in sorted(page_buckets[page], reverse=True):
            parts = sorted(page_buckets[page][y], key=lambda item: item.x)
            line_text = ""
            fonts: List[str] = []
            prev_end: Optional[float] = None
            min_x = parts[0].x
            for part in parts:
                fonts.append(part.font)
                if prev_end is not None and part.x - prev_end > line_merge_gap:
                    line_text += " "
                line_text += part.text
                prev_end = part.x + (7 * len(part.text))
            text = normalize_text(line_text)
            if text:
                lines.append(
                    Line(
                        page=page,
                        x=min_x,
                        y=y,
                        font=",".join(sorted(set(fonts))),
                        text=text,
                    )
                )
    return lines


def is_header_or_footer(line: Line, args: argparse.Namespace) -> bool:
    text = line.text.strip()
    if line.y >= args.header_y_min or line.y <= args.footer_y_max:
        return True
    if re.fullmatch(r"\d+\.\.?", text):
        return True
    if text.startswith("Draft "):
        return True
    if text in {"(MORE)", "(MORE)(MORE)", "*"}:
        return True
    return False


def classify_line(line: Line, args: argparse.Namespace) -> Optional[Line]:
    if is_header_or_footer(line, args):
        return None

    text = line.text.strip()
    if text == "*":
        return None
    if text in {"(CONT’D)", "(CONT'D)", "(CONT�D)"}:
        line.kind = "continuation"
        return line
    if line.x >= args.transition_min_x and is_transition(text):
        line.kind = "transition"
        return line
    if line.x >= args.character_min_x and is_character_cue(text):
        line.kind = "character"
        return line
    if line.x >= args.parenthetical_min_x and is_parenthetical_text(text):
        line.kind = "parenthetical"
        return line
    if line.x >= args.dialogue_min_x:
        line.kind = "dialogue"
        return line
    if line.x <= args.scene_max_x and is_scene_heading(text):
        line.kind = "scene"
        return line
    line.kind = "action"
    return line


def merge_same_kind(lines: List[Line]) -> List[Line]:
    merged: List[Line] = []
    for line in lines:
        if (
            merged
            and merged[-1].page == line.page
            and merged[-1].kind == line.kind
            and merged[-1].x == line.x
            and merged[-1].kind in {"action", "dialogue"}
        ):
            merged[-1].text = normalize_text(f"{merged[-1].text} {line.text}")
            merged[-1].y = min(merged[-1].y, line.y)
            continue
        merged.append(line)
    return merged


def build_blocks(lines: List[Line]) -> List[Block]:
    blocks: List[Block] = []
    current_dialogue: Optional[Block] = None

    for line in lines:
        if line.kind == "continuation":
            continue
        if line.kind == "character":
            current_dialogue = Block(kind="dialogue", speaker=line.text)
            blocks.append(current_dialogue)
            continue
        if line.kind == "parenthetical" and current_dialogue is not None:
            current_dialogue.parenthetical = line.text
            current_dialogue.lines.append(line)
            continue
        if line.kind == "dialogue" and current_dialogue is not None:
            current_dialogue.lines.append(line)
            continue

        current_dialogue = None
        blocks.append(Block(kind=line.kind, lines=[line]))

    return [block for block in blocks if block.lines or block.speaker]


def extract_title_page_lines(lines: List[Line]) -> List[Line]:
    first_page_lines = [line for line in lines if line.page == 1]
    if not first_page_lines:
        return []
    for idx, line in enumerate(first_page_lines):
        if is_scene_heading(line.text):
            return first_page_lines[:idx]
    return first_page_lines


def exclude_title_page_lines(lines: List[Line], title_page_lines: List[Line]) -> List[Line]:
    if not title_page_lines:
        return lines
    title_page_keys = {(line.page, line.x, line.y, line.text) for line in title_page_lines}
    return [
        line for line in lines if (line.page, line.x, line.y, line.text) not in title_page_keys
    ]


def infer_metadata(
    lines: List[Line], pdf_path: Path, title_override: Optional[str], author_override: Optional[str]
) -> tuple[str, str]:
    title = title_override
    author = author_override

    if not title:
        for line in lines[:8]:
            if line.page == 1 and line.text and uppercase_ratio(line.text) > 0.7:
                title = line.text.title() if line.text.isupper() else line.text
                break

    if not author:
        for idx, line in enumerate(lines[:12]):
            if line.text.lower() == "screenplay by" and idx + 1 < len(lines):
                author = lines[idx + 1].text
                break

    if not title:
        title = pdf_path.stem
    if not author:
        author = "Unknown"
    return title, author


def slugify_text(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def render_xhtml(
    title: str, author: str, blocks: List[Block]
) -> tuple[str, List[TocEntry]]:
    body_parts = []
    toc_entries: List[TocEntry] = []
    scene_counts: dict[str, int] = {}

    for block in blocks:
        if block.kind == "scene":
            scene_text = block.lines[0].text
            scene_slug = slugify_text(scene_text)
            scene_counts[scene_slug] = scene_counts.get(scene_slug, 0) + 1
            scene_id = (
                scene_slug if scene_counts[scene_slug] == 1 else f"{scene_slug}-{scene_counts[scene_slug]}"
            )
            body_parts.append(f'<h2 id="{html.escape(scene_id)}">{html.escape(scene_text)}</h2>')
            toc_entries.append(TocEntry(href=f"text.xhtml#{scene_id}", label=scene_text))
        elif block.kind == "transition":
            body_parts.append(f'<p class="transition">{html.escape(block.lines[0].text)}</p>')
        elif block.kind == "action":
            body_parts.append(f'<p class="action">{html.escape(block.lines[0].text)}</p>')
        elif block.kind == "dialogue":
            dialogue_text = " ".join(line.text for line in block.lines if line.kind == "dialogue")
            paren = (
                f'<p class="parenthetical">{html.escape(block.parenthetical)}</p>'
                if block.parenthetical
                else ""
            )
            body_parts.append(
                f'<div class="dialogue-block"><p class="speaker">{html.escape(block.speaker or "")}</p>{paren}<p class="dialogue">{html.escape(dialogue_text)}</p></div>'
            )

    body = "\n".join(body_parts)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
  <head>
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" type="text/css" href="styles.css"/>
  </head>
  <body>
{textwrap.indent(body, '    ')}
  </body>
</html>
""", toc_entries


def render_title_page_xhtml(title: str, title_page_lines: List[Line]) -> str:
    body_parts = []
    previous_y: Optional[float] = None
    for line in title_page_lines:
        margin_top = 0.0
        if previous_y is not None:
            gap = max(previous_y - line.y, 0.0)
            margin_top = min(gap / 18.0, 2.5)
        body_parts.append(
            f'<p class="title-line" style="margin-top: {margin_top:.2f}em;">{html.escape(line.text)}</p>'
        )
        previous_y = line.y

    body = "\n".join(body_parts)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
  <head>
    <title>{html.escape(title)} Title Page</title>
    <link rel="stylesheet" type="text/css" href="styles.css"/>
  </head>
  <body class="title-page-body">
    <section class="title-page">
{textwrap.indent(body, '      ')}
    </section>
  </body>
</html>
"""


def render_cover_xhtml(title: str, cover_href: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
  <head>
    <title>{html.escape(title)} Cover</title>
    <style>
      body {{ margin: 0; padding: 0; }}
      img {{ display: block; width: 100%; height: auto; }}
    </style>
  </head>
  <body>
    <img src="{html.escape(cover_href)}" alt="{html.escape(title)} cover"/>
  </body>
</html>
"""


def render_nav_xhtml(title: str, toc_entries: List[TocEntry]) -> str:
    toc_items = "\n".join(
        f'      <li><a href="{html.escape(entry.href)}">{html.escape(entry.label)}</a></li>'
        for entry in toc_entries
    )
    if not toc_items:
        toc_items = f'      <li><a href="text.xhtml">{html.escape(title)}</a></li>'
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body>
    <nav epub:type="toc" id="toc">
      <h1>Contents</h1>
      <ol>
{toc_items}
      </ol>
    </nav>
  </body>
</html>
"""


def build_epub(
    output_path: Path,
    title: str,
    author: str,
    title_page_xhtml: Optional[str],
    xhtml: str,
    toc_entries: List[TocEntry],
    cover_path: Optional[Path] = None,
) -> None:
    styles = """
body { font-family: Georgia, serif; margin: 5%; line-height: 1.4; }
h1 { text-align: center; margin-bottom: 0.2em; }
.byline { text-align: center; margin-top: 0; margin-bottom: 2em; font-style: italic; }
h2 { margin-top: 1.8em; margin-bottom: 0.8em; font-size: 1em; letter-spacing: 0.05em; text-transform: uppercase; }
.action { margin: 0.6em 0; }
.transition { margin: 1em 0; text-align: right; font-weight: bold; }
.dialogue-block { margin: 0.8em 0 1em; }
.speaker { margin: 0; text-align: center; font-variant: small-caps; font-weight: bold; }
.parenthetical { margin: 0.15em 0; text-align: center; font-style: italic; }
.dialogue { margin: 0.15em 12% 0.8em 12%; }
.title-page-body { margin: 0; }
.title-page {
  min-height: 100vh;
  padding: 8vh 8% 10vh;
  display: flex;
  flex-direction: column;
  justify-content: center;
  text-align: center;
}
.title-line { margin-bottom: 0; }
""".strip()

    cover_bytes = None
    cover_href = None
    cover_media_type = None
    cover_xhtml = None
    if cover_path is not None:
        cover_bytes = cover_path.read_bytes()
        cover_media_type, cover_extension = detect_cover_format(cover_path)
        cover_href = f"images/cover{cover_extension}"
        cover_xhtml = render_cover_xhtml(title, cover_href)

    cover_metadata = ""
    cover_manifest = ""
    cover_spine = ""
    if cover_path is not None and cover_href and cover_media_type:
        cover_metadata = '    <meta name="cover" content="cover-image"/>\n'
        cover_manifest = (
            '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n'
            f'    <item id="cover-image" href="{html.escape(cover_href)}" '
            f'media-type="{cover_media_type}" properties="cover-image"/>\n'
        )
        cover_spine = '    <itemref idref="cover"/>\n'

    title_page_manifest = ""
    title_page_spine = ""
    if title_page_xhtml is not None:
        title_page_manifest = '    <item id="titlepage" href="titlepage.xhtml" media-type="application/xhtml+xml"/>\n'
        title_page_spine = '    <itemref idref="titlepage"/>\n'

    content_opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{html.escape(title)}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>en</dc:language>
{cover_metadata.rstrip()}
  </metadata>
  <manifest>
{cover_manifest.rstrip()}
{title_page_manifest.rstrip()}
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="styles.css" media-type="text/css"/>
  </manifest>
  <spine>
{cover_spine.rstrip()}
{title_page_spine.rstrip()}
    <itemref idref="nav"/>
    <itemref idref="text"/>
  </spine>
</package>
"""

    nav_xhtml = render_nav_xhtml(title, toc_entries)

    container_xml = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container_xml)
        archive.writestr("OEBPS/content.opf", content_opf)
        if cover_xhtml is not None and cover_href is not None and cover_bytes is not None:
            archive.writestr("OEBPS/cover.xhtml", cover_xhtml)
            archive.writestr(f"OEBPS/{cover_href}", cover_bytes)
        if title_page_xhtml is not None:
            archive.writestr("OEBPS/titlepage.xhtml", title_page_xhtml)
        archive.writestr("OEBPS/nav.xhtml", nav_xhtml)
        archive.writestr("OEBPS/text.xhtml", xhtml)
        archive.writestr("OEBPS/styles.css", styles)


def main() -> int:
    args = parse_args()
    if not args.input_pdf.exists():
        print(f"Input PDF not found: {args.input_pdf}", file=sys.stderr)
        return 1
    if args.cover and not args.cover.exists():
        print(f"Cover image not found: {args.cover}", file=sys.stderr)
        return 1
    if args.cover:
        try:
            detect_cover_format(args.cover)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    output = args.output or args.input_pdf.with_suffix(".epub")
    fragments = extract_fragments(args.input_pdf)
    if not fragments:
        print("No extractable text found. This tool expects a text-based PDF.", file=sys.stderr)
        return 1

    lines = group_lines(fragments, line_merge_gap=args.line_merge_gap)
    title_page_lines = extract_title_page_lines(
        [line for line in lines if not is_header_or_footer(line, args)]
    )
    classified = []
    for line in lines:
        maybe = classify_line(line, args)
        if maybe is not None:
            classified.append(maybe)
    merged = merge_same_kind(classified)
    metadata_lines = merged[:]
    merged = exclude_title_page_lines(merged, title_page_lines)

    if args.debug_lines:
        for line in merged:
            print(
                f"p{line.page} x={line.x:6.1f} y={line.y:6.1f} {line.kind:12} {line.text}",
                file=sys.stderr,
            )

    title, author = infer_metadata(metadata_lines, args.input_pdf, args.title, args.author)
    blocks = build_blocks(merged)
    title_page_xhtml = render_title_page_xhtml(title, title_page_lines) if title_page_lines else None
    xhtml, toc_entries = render_xhtml(title, author, blocks)
    build_epub(output, title, author, title_page_xhtml, xhtml, toc_entries, args.cover)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
