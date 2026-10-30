"""Import a saved rendered portal page without executing or fetching content.

This is a structural capture, not a clinical interpretation. Source text stays
data, including any prose that resembles instructions. The complete original
HTML is preserved even when extraction fails. Embedded documents are listed for
the capture process to preserve separately; this module never follows links.
"""

from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from typing import Iterator

from .store import Store


SYSTEM = "atlantic-health-mychart"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
NON_RENDERED = {"script", "style", "noscript", "template"}
BLOCK = {"address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "ul"}


@dataclass
class Node:
    tag: str
    attributes: dict[str, str | None] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    locator: str = ""


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.document = Node("document")
        self.stack = [self.document]

    def handle_starttag(self, tag, attributes):
        parent = self.stack[-1]
        index = 1 + sum(isinstance(child, Node) and child.tag == tag for child in parent.children)
        node = Node(tag, dict(attributes), locator=f"{parent.locator}/{tag}[{index}]")
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        # Script/configuration bytes remain only in the original source artifact.
        if not any(node.tag in NON_RENDERED for node in self.stack):
            self.stack[-1].children.append(data)


def _hidden(node: Node) -> bool:
    attributes = node.attributes
    style = (attributes.get("style") or "").lower()
    return (
        node.tag in NON_RENDERED
        or "hidden" in attributes
        or (attributes.get("aria-hidden") or "").lower() == "true"
        or (node.tag == "input" and (attributes.get("type") or "").lower() == "hidden")
        or re.search(r"(?:^|;)\s*display\s*:\s*none\s*(?:!important\s*)?(?:;|$)", style) is not None
        or re.search(r"(?:^|;)\s*visibility\s*:\s*(?:hidden|collapse)\s*(?:!important\s*)?(?:;|$)", style) is not None
    )


def _nodes(node: Node, visible=True) -> Iterator[Node]:
    if visible and _hidden(node):
        return
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _nodes(child, visible)


def _segments(node: Node) -> list[dict[str, str]]:
    if _hidden(node):
        return []
    result = []
    for child in node.children:
        if isinstance(child, str):
            result.append({"locator": node.locator, "text": child})
        else:
            result.extend(_segments(child))
    return result


def _render(node: Node | str) -> str:
    if isinstance(node, str):
        return node
    if _hidden(node):
        return ""
    if node.tag in ("br", "hr"):
        return "\n"
    if node.tag == "tr":
        cells = [child for child in node.children if isinstance(child, Node) and child.tag in ("td", "th") and not _hidden(child)]
        if cells:
            return "\n" + "\t".join(_render(cell).strip() for cell in cells) + "\n"
    content = "".join(_render(child) for child in node.children)
    return "\n" + content + "\n" if node.tag in BLOCK else content


def _text(node: Node) -> str:
    # Exact decoded text nodes are also retained in text_segments. This version
    # adds structural separators for reading, including tabs between table cells.
    value = "\n".join(line.strip(" \r\f\v") for line in _render(node).splitlines()).strip()
    return re.sub(r"\n{3,}", "\n\n", value)


def _table(table: Node) -> dict:
    rows = []

    def visit(node: Node, section: str | None = None):
        if _hidden(node) or (node.tag == "table" and node is not table):
            return
        if node.tag in ("thead", "tbody", "tfoot"):
            section = node.tag
        if node.tag == "tr":
            cells = [child for child in node.children if isinstance(child, Node) and child.tag in ("td", "th") and not _hidden(child)]
            rows.append({"locator": node.locator, "attributes": node.attributes, "section": section,
                         "cells": [{"tag": cell.tag, "locator": cell.locator, "attributes": cell.attributes,
                                    "text": _text(cell), "text_segments": _segments(cell)} for cell in cells]})
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child, section)

    visit(table)
    caption = next((child for child in table.children if isinstance(child, Node) and child.tag == "caption" and not _hidden(child)), None)
    return {"locator": table.locator, "attributes": table.attributes,
            "caption": _text(caption) if caption else None, "rows": rows}


def _decode(content: bytes) -> tuple[str, str]:
    if content.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        meta = re.search(br"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([a-zA-Z0-9._-]+)", content[:16384], re.I)
        encoding = meta.group(1).decode("ascii") if meta else "utf-8"
    try:
        value = content.decode(encoding, errors="strict")
    except (UnicodeError, LookupError) as error:
        raise ValueError("Saved HTML encoding is unsupported or invalid; original artifact preserved") from error
    return value, encoding


def parse_html(content: bytes, *, document_body=False) -> dict:
    decoded, encoding = _decode(content)
    parser = DocumentParser()
    parser.feed(decoded)
    parser.close()
    visible = list(_nodes(parser.document))
    candidates = []
    for node in visible:
        identifier = (node.attributes.get("id") or "").lower()
        if node.tag == "main":
            candidates.append((0 if identifier == "main" else 1, node))
        elif "main" in (node.attributes.get("role") or "").lower().split():
            candidates.append((2, node))
        elif identifier in ("main", "maincontent", "main-content"):
            candidates.append((3, node))
    if not candidates and document_body:
        candidates=[(4,node) for node in visible if node.tag=='body']
    if not candidates:
        raise ValueError("Saved HTML has no explicit main content region; original artifact preserved")
    main = min(candidates, key=lambda item: item[0])[1]
    nodes = list(_nodes(main))
    titles = [node for node in _nodes(parser.document, visible=False) if node.tag == "title"]
    title = _text(titles[0]) if titles else None
    headings = [{"level": int(node.tag[1:]), "locator": node.locator, "attributes": node.attributes, "text": _text(node)}
                for node in nodes if re.fullmatch(r"h[1-6]", node.tag)]
    links = [{"locator": node.locator, "attributes": node.attributes, "href": node.attributes.get("href"), "text": _text(node)}
             for node in nodes if node.tag == "a"]
    controls = [{"tag": node.tag, "locator": node.locator, "attributes": node.attributes, "text": _text(node),
                 "options": [{"attributes": option.attributes, "text": _text(option)} for option in _nodes(node) if option.tag == "option"]}
                for node in nodes if node.tag in ("input", "textarea", "select", "button", "output")]
    embedded = [{"tag": node.tag, "locator": node.locator, "attributes": node.attributes}
                for node in nodes if node.tag in ("iframe", "object", "embed")]
    images = [{"locator": node.locator, "attributes": node.attributes} for node in nodes if node.tag == "img"]
    warnings = ["CSS visibility and interaction state are not evaluated; this captures the saved DOM state only."]
    if len(candidates) > 1:
        warnings.append("Multiple main-region candidates exist; the explicit main element is preferred.")
    if embedded:
        warnings.append("Embedded documents are references only and require separate artifact capture/import.")
    if images:
        warnings.append("Image references and alt attributes are retained; image pixels are not extracted.")
    return {"title": title, "label": next((item["text"] for item in headings if item["text"]), title),
            "main_region": {"tag": main.tag, "locator": main.locator, "attributes": main.attributes},
            "text": _text(main), "text_segments": _segments(main), "headings": headings,
            "tables": [_table(node) for node in nodes if node.tag == "table"], "links": links,
            "controls": controls, "embedded_documents": embedded, "images": images,
            "extraction": {"extractor": "health_bank.portal", "version": 1, "encoding": encoding,
                           "scope": "saved document body" if main.tag=='body' else "saved main content region", "warnings": warnings}}


def import_html(root, path, lane, patient_id="patient-001", source_url=None, *, document_body=False) -> dict:
    path = Path(path)
    if path.suffix.lower() not in (".html", ".htm"):
        raise ValueError("Portal import requires an .html or .htm source file")
    store = Store(root)
    receipt = store.add_artifact(path, SYSTEM, source_url, "saved-portal-html")
    data = parse_html((store.root / receipt["local_path"]).read_bytes(),document_body=document_body)
    data["source_url"] = source_url
    store.add_record({"patient_id": patient_id, "lane": lane, "kind": "portal_surface",
                      "source": {"system": SYSTEM, "artifact_sha256": receipt["artifact_sha256"],
                                 "locator": data["main_region"]["locator"],
                                 "record_key": "portal_surface:" + (source_url or path.name)},
                      "effective_time": None, "status": "unknown", "data": data})
    return {"artifact_sha256": receipt["artifact_sha256"], "records_processed": 1, "by_lane": {lane: 1},
            "headings": len(data["headings"]), "tables": len(data["tables"]), "links": len(data["links"]),
            "embedded_documents": len(data["embedded_documents"]), "text_characters": len(data["text"]),
            "warnings": len(data["extraction"]["warnings"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import saved portal main content as evidence without following links")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--lane", required=True)
    parser.add_argument("--patient-id", default="patient-001")
    parser.add_argument("--source-url")
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = import_html(arguments.root, arguments.path, arguments.lane, arguments.patient_id, arguments.source_url)
    except (OSError, ValueError, TypeError) as error:
        print(f"health-bank portal: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
