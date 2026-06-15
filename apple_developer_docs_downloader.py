#!/usr/bin/env python3
"""
Download any Apple developer documentation as organized markdown files.

Usage:
    python3 apple_developer_docs_downloader.py https://developer.apple.com/documentation/vision
    python3 apple_developer_docs_downloader.py https://developer.apple.com/documentation/accessibility
    python3 apple_developer_docs_downloader.py vision
    python3 apple_developer_docs_downloader.py AppleArchive
    python3 apple_developer_docs_downloader.py -o my-docs vision  # custom output directory
    python3 apple_developer_docs_downloader.py -w 6 vision        # 6 parallel workers
"""

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import threading
import urllib.request
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path

BASE_URL = "https://developer.apple.com/tutorials/data/documentation"
MAX_RETRIES = 3
# Hard cap on a single API response held in memory. Real documentation pages are
# at most a few MB; this only guards against a misbehaving proxy or wrong URL
# returning an unbounded stream that would exhaust memory across many workers.
MAX_RESPONSE_BYTES = 50 * 1024 * 1024

lock = threading.Lock()
visited = set()
failed = []
# Paths that returned HTTP 404 — permanently missing, not transient failures.
# Tracked separately and persisted so a resumed crawl doesn't re-fetch every
# known-missing page on each run: they have no manifest entry, so the
# pending-recovery logic in main() would otherwise re-enqueue them forever.
gone = set()
manifest = {}
page_count = 0
# Case-normalized output path -> the api_path that owns it. Used to detect when
# two distinct pages resolve to the same file (case-insensitive filesystem or
# after filename sanitization) so the second one gets a disambiguated name
# instead of silently overwriting the first.
allocated_paths = {}

# Set after we fetch the root page and discover the identifier scheme
DOC_IDENTIFIER_PREFIX = None  # e.g. "doc://Vision/documentation/"
FRAMEWORK_API_PATH = None  # e.g. "Vision" (case as returned by the API)
OUTPUT_DIR = None
STATE_FILE = None
MANIFEST_FILE = None


def fetch_json(path, retry=0):
    url = f"{BASE_URL}/{path}.json"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            if length is not None and int(length) > MAX_RESPONSE_BYTES:
                raise ValueError(f"response too large: {length} bytes")
            # Read one byte past the cap so a missing or lying Content-Length is
            # still bounded; reject anything that actually exceeds it.
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("response exceeded size cap")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # A 404 is a genuinely-missing page, not a failure: Apple's topic
            # sections cross-link to beta-only or withdrawn symbols. Skip it
            # quietly rather than recording it in `failed`, which would force a
            # non-zero exit on most large frameworks. Record it in `gone` so a
            # resumed crawl can tell a known-missing page (never to be retried)
            # apart from one merely interrupted before it completed.
            with lock:
                gone.add(path)
            return None
        if retry < MAX_RETRIES:
            delay = 2**retry
            print(f"  HTTP {e.code} for {url}, retrying in {delay}s...")
            time.sleep(delay)
            return fetch_json(path, retry + 1)
        print(f"  FAILED: HTTP {e.code} for {url}")
        with lock:
            failed.append({"path": path, "error": f"HTTP {e.code}"})
        return None
    except Exception as e:
        # Retry on any non-HTTP error here, including JSONDecodeError: a
        # truncated or momentarily-malformed body is usually transient, and a
        # re-fetch typically returns valid JSON. Genuinely permanent failures
        # still give up after MAX_RETRIES and are recorded in `failed`.
        if retry < MAX_RETRIES:
            delay = 2**retry
            print(f"  Error for {url}: {e}, retrying in {delay}s...")
            time.sleep(delay)
            return fetch_json(path, retry + 1)
        print(f"  FAILED: {e} for {url}")
        with lock:
            failed.append({"path": path, "error": str(e)})
        return None


# ---------------------------------------------------------------------------
# Markdown rendering (framework-agnostic)
# ---------------------------------------------------------------------------


def render_inline(items):
    if not items:
        return ""
    parts = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
            continue
        t = item.get("type", "")
        text = item.get("text", "")
        if t == "text":
            parts.append(text)
        elif t == "codeVoice":
            parts.append(f"`{item.get('code', text)}`")
        elif t == "emphasis":
            parts.append(f"*{render_inline(item.get('inlineContent', []))}*")
        elif t == "strong":
            parts.append(f"**{render_inline(item.get('inlineContent', []))}**")
        elif t == "reference":
            inner = render_inline(item.get("inlineContent", [])) or item.get(
                "title", item.get("identifier", "").split("/")[-1]
            )
            parts.append(f"`{inner}`")
        elif t == "newTerm":
            parts.append(f"*{render_inline(item.get('inlineContent', []))}*")
        elif t == "link":
            parts.append(f"[{item.get('title', '')}]({item.get('destination', '')})")
        elif t == "image":
            parts.append(f"[Image: {item.get('alt', '')}]")
        elif t == "superscript":
            parts.append(f"<sup>{render_inline(item.get('inlineContent', []))}</sup>")
        elif t == "subscript":
            parts.append(f"<sub>{render_inline(item.get('inlineContent', []))}</sub>")
        elif t == "strikethrough":
            parts.append(f"~~{render_inline(item.get('inlineContent', []))}~~")
        else:
            parts.append(text or render_inline(item.get("inlineContent", [])))
    return "".join(parts)


def render_content_block(block, depth=0):
    # Recurses with render_inline for nested lists/asides/tables. No explicit
    # depth cap is needed: the input is Apple's own documentation JSON (trusted,
    # fetched over HTTPS) whose nesting is shallow, and any pathological page
    # that did exceed the recursion limit is caught per-page in crawl_parallel
    # and recorded as a failure rather than crashing the whole crawl.
    t = block.get("type", "")
    lines = []

    if t == "heading":
        level = block.get("level", 2)
        text = render_inline(block.get("inlineContent", []))
        lines.append(f"{'#' * (level + 1)} {text}")
        lines.append("")
    elif t == "paragraph":
        lines.append(render_inline(block.get("inlineContent", [])))
        lines.append("")
    elif t == "codeListing":
        # Strip anything that isn't a bare language token so an unexpected syntax
        # value (spaces, backticks) can't malform the fence info string.
        lang = re.sub(r"[^A-Za-z0-9_+.-]", "", block.get("syntax", "swift")) or "swift"
        code = "\n".join(block.get("code", []))
        # Pick a fence longer than the longest backtick run in the code so a
        # snippet that itself contains ``` can't close the block early.
        longest_run = max((len(r) for r in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest_run + 1)
        lines.append(f"{fence}{lang}")
        lines.append(code)
        lines.append(fence)
        lines.append("")
    elif t == "unorderedList":
        for item in block.get("items", []):
            for i, content in enumerate(item.get("content", [])):
                rendered = render_content_block(content, depth + 1).rstrip()
                if i == 0:
                    indent = "  " * depth
                    # Indent continuation lines so multi-line content (e.g. a code
                    # block) stays inside the list item instead of breaking out as
                    # a top-level block.
                    rendered = rendered.replace("\n", "\n" + indent + "  ")
                    lines.append(f"{indent}- {rendered}")
                else:
                    indent = "  " * (depth + 1)
                    rendered = rendered.replace("\n", "\n" + indent)
                    lines.append(f"{indent}{rendered}")
        lines.append("")
    elif t == "orderedList":
        for idx, item in enumerate(block.get("items", []), 1):
            for i, content in enumerate(item.get("content", [])):
                rendered = render_content_block(content, depth + 1).rstrip()
                if i == 0:
                    indent = "  " * depth
                    # Indent continuation lines so multi-line content stays inside
                    # the list item instead of breaking out as a top-level block.
                    rendered = rendered.replace("\n", "\n" + indent + "  ")
                    lines.append(f"{indent}{idx}. {rendered}")
                else:
                    indent = "  " * (depth + 1)
                    rendered = rendered.replace("\n", "\n" + indent)
                    lines.append(f"{indent}{rendered}")
        lines.append("")
    elif t == "aside":
        name = block.get("name", block.get("style", "note")).capitalize()
        lines.append(f"> **{name}:**")
        for content in block.get("content", []):
            for line in render_content_block(content).rstrip().split("\n"):
                lines.append(f"> {line}")
        lines.append("")
    elif t == "table":
        rows = block.get("rows", [])
        if rows:
            first_row = rows[0]
            header_cells = []
            for cell in first_row:
                cell_parts = [render_content_block(c).strip() for c in cell]
                cell_text = " ".join(cell_parts).replace("\n", " ").replace("|", "\\|")
                header_cells.append(cell_text)
            lines.append("| " + " | ".join(header_cells) + " |")
            lines.append("| " + " | ".join(["---"] * len(header_cells)) + " |")
            for row in rows[1:]:
                row_cells = []
                for cell in row:
                    cell_parts = [render_content_block(c).strip() for c in cell]
                    cell_text = " ".join(cell_parts).replace("\n", " ").replace("|", "\\|")
                    row_cells.append(cell_text)
                lines.append("| " + " | ".join(row_cells) + " |")
            lines.append("")
    elif t == "termList":
        for item in block.get("items", []):
            term = render_inline(item.get("term", {}).get("inlineContent", []))
            lines.append(f"**{term}**")
            for content in item.get("definition", {}).get("content", []):
                lines.append(f"  {render_content_block(content).rstrip()}")
        lines.append("")
    elif t == "dictionaryExample":
        for content in block.get("content", []):
            lines.append(render_content_block(content).rstrip())
        lines.append("")
    elif t == "links":
        for item in block.get("items", []):
            lines.append(f"- `{item}`")
        lines.append("")
    elif t == "small":
        lines.append(render_inline(block.get("inlineContent", [])))
        lines.append("")
    else:
        if "inlineContent" in block:
            text = render_inline(block.get("inlineContent", []))
            if text:
                lines.append(text)
                lines.append("")
        elif "content" in block:
            for content in block.get("content", []):
                lines.append(render_content_block(content).rstrip())
            lines.append("")

    return "\n".join(lines)


def render_declaration(section):
    lines = ["## Declaration", ""]
    for decl in section.get("declarations", []):
        platforms = decl.get("platforms", [])
        if platforms:
            lines.append(f"*Platforms: {', '.join(platforms)}*")
            lines.append("")
        tokens = decl.get("tokens", [])
        code = "".join(t.get("text", "") for t in tokens)
        lines.append("```swift")
        lines.append(code)
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def render_parameters(section):
    lines = ["## Parameters", ""]
    for param in section.get("parameters", []):
        name = param.get("name", "")
        desc = " ".join(
            render_content_block(c).strip() for c in param.get("content", [])
        )
        # Indent any continuation lines so a multi-line description stays inside
        # the list item instead of breaking out as a top-level block.
        desc = desc.replace("\n", "\n  ")
        lines.append(f"- **`{name}`**: {desc}")
    lines.append("")
    return "\n".join(lines)


def render_possible_values(section):
    lines = ["## Possible Values", ""]
    for val in section.get("values", []):
        name = val.get("name", "")
        desc = " ".join(render_content_block(c).strip() for c in val.get("content", []))
        if desc:
            # Keep continuation lines indented under the list item.
            desc = desc.replace("\n", "\n  ")
            lines.append(f"- **`{name}`**: {desc}")
        else:
            lines.append(f"- **`{name}`**")
    lines.append("")
    return "\n".join(lines)


def convert_to_markdown(data, api_path):
    lines = []
    metadata = data.get("metadata", {})
    title = metadata.get("title", "Untitled")
    role_heading = metadata.get("roleHeading", "")

    lines.append(f"# {title}")
    lines.append("")

    if role_heading:
        lines.append(f"**{role_heading}**")
        lines.append("")

    platforms = metadata.get("platforms", [])
    if platforms:
        plat_strs = []
        for p in platforms:
            name = p.get("name", "")
            intro = p.get("introducedAt", "")
            if name and intro:
                plat_strs.append(f"{name} {intro}+")
            elif name:
                plat_strs.append(name)
        if plat_strs:
            lines.append(f"**Availability:** {', '.join(plat_strs)}")
            lines.append("")

    modules = metadata.get("modules", [])
    if modules:
        mod_names = [m.get("name", "") for m in modules if m.get("name")]
        if mod_names:
            lines.append(f"**Framework:** {', '.join(mod_names)}")
            lines.append("")

    abstract = data.get("abstract", [])
    if abstract:
        lines.append(render_inline(abstract))
        lines.append("")

    for section in data.get("primaryContentSections", []):
        kind = section.get("kind", "")
        if kind == "declarations":
            lines.append(render_declaration(section))
        elif kind == "parameters":
            lines.append(render_parameters(section))
        elif kind == "possibleValues":
            lines.append(render_possible_values(section))
        elif kind == "content":
            for block in section.get("content", []):
                lines.append(render_content_block(block))
        elif kind == "details":
            for key, val in section.get("details", {}).items():
                if isinstance(val, list):
                    for v in val:
                        lines.append(render_content_block(v))
                elif isinstance(val, dict):
                    lines.append(render_content_block(val))
        elif kind == "attributes":
            lines.append("## Attributes")
            lines.append("")
            for attr in section.get("attributes", []):
                lines.append(f"- **{attr.get('title', '')}**: {attr.get('value', '')}")
            lines.append("")
        elif kind == "relationships":
            lines.append("## Relationships")
            lines.append("")
            for rel in section.get("relationships", []):
                items = rel.get("identifiers", [])
                if items:
                    names = ["`" + i.split("/")[-1] + "`" for i in items]
                    lines.append(f"**{rel.get('type', '')}:** {', '.join(names)}")
            lines.append("")
        elif kind == "properties":
            lines.append("## Properties")
            lines.append("")
            for prop in section.get("items", []):
                lines.append(render_content_block(prop))

    for section in data.get("relationshipsSections", []):
        rel_type = section.get("type", section.get("title", "Relationships"))
        identifiers = section.get("identifiers", [])
        if identifiers:
            heading = rel_type.replace("conformsTo", "Conforms To").replace(
                "inheritsFrom", "Inherits From"
            )
            lines.append(f"## {heading}")
            lines.append("")
            for ident in identifiers:
                lines.append(f"- `{ident.split('/')[-1]}`")
            lines.append("")

    topic_sections = data.get("topicSections", [])
    if topic_sections:
        lines.append("## Topics")
        lines.append("")
        refs = data.get("references", {})
        for section in topic_sections:
            lines.append(f"### {section.get('title', '')}")
            lines.append("")
            for ident in section.get("identifiers", []):
                ref = refs.get(ident, {})
                ref_title = ref.get("title", ident.split("/")[-1])
                ref_abstract = render_inline(ref.get("abstract", []))
                fragments = ref.get("fragments", [])
                frag_text = "".join(f.get("text", "") for f in fragments)
                if frag_text and frag_text != ref_title:
                    lines.append(f"- `{frag_text}`")
                else:
                    lines.append(f"- `{ref_title}`")
                if ref_abstract:
                    lines.append(f"  {ref_abstract}")
            lines.append("")

    see_also = data.get("seeAlsoSections", [])
    if see_also:
        lines.append("## See Also")
        lines.append("")
        refs = data.get("references", {})
        for section in see_also:
            section_title = section.get("title", "")
            if section_title:
                lines.append(f"### {section_title}")
                lines.append("")
            for ident in section.get("identifiers", []):
                ref = refs.get(ident, {})
                lines.append(f"- `{ref.get('title', ident.split('/')[-1])}`")
            lines.append("")

    default_impls = data.get("defaultImplementationsSections", [])
    if default_impls:
        lines.append("## Default Implementations")
        lines.append("")
        refs = data.get("references", {})
        for section in default_impls:
            section_title = section.get("title", "")
            if section_title:
                lines.append(f"### {section_title}")
                lines.append("")
            for ident in section.get("identifiers", []):
                ref = refs.get(ident, {})
                lines.append(f"- `{ref.get('title', ident.split('/')[-1])}`")
            lines.append("")

    result = "\n".join(lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() + "\n"


# ---------------------------------------------------------------------------
# File-path helpers
# ---------------------------------------------------------------------------


def classify_symbol(metadata, path):
    role = metadata.get("role", "")
    symbol_kind = metadata.get("symbolKind", "").lower()

    if role in ("article", "sampleCode"):
        return "articles"
    if role == "collection" and "original-objective-c" in path.lower():
        return "legacy"

    kind_map = {
        "struct": "structures",
        "class": "classes",
        "enum": "enumerations",
        "protocol": "protocols",
        "typealias": "type-aliases",
        "func": "functions",
        "method": "methods",
        "property": "properties",
        "case": "cases",
        "init": "initializers",
        "op": "operators",
        "var": "variables",
        "macro": "macros",
        "associatedtype": "associated-types",
        "tdef": "type-definitions",
        "module": "",
    }
    return kind_map.get(symbol_kind, "other")


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", "-", name)
    name = name.strip(".-_")
    if not name:
        return "untitled"
    # Truncate by UTF-8 byte length, not character count: many filesystems
    # (APFS, ext4) cap names at 255 bytes and one character can be several bytes.
    encoded = name.encode("utf-8")
    if len(encoded) > 200:
        # Re-strip after truncation: dropping a partial multibyte sequence (or
        # cutting mid-name) can leave a trailing/leading "._-" or an empty
        # string, both of which make awkward or invalid filenames.
        truncated = encoded[:200].decode("utf-8", "ignore").strip(".-_")
        return truncated or "untitled"
    return name


def strip_framework_prefix(api_path):
    """Remove the framework name from the front of an API path."""
    # Compare case-insensitively: the framework arg may be lower-case (e.g.
    # "vision") while Apple's identifiers use canonical case ("Vision/...").
    if FRAMEWORK_API_PATH and api_path.lower().startswith(FRAMEWORK_API_PATH.lower() + "/"):
        return api_path[len(FRAMEWORK_API_PATH) + 1 :]
    if FRAMEWORK_API_PATH and api_path.lower() == FRAMEWORK_API_PATH.lower():
        return ""
    return api_path


def get_output_path(data, api_path):
    relative = strip_framework_prefix(api_path)

    if not relative:
        return OUTPUT_DIR / "index.md"

    metadata = data.get("metadata", {})
    parts = relative.split("/")

    if len(parts) == 1:
        category = classify_symbol(metadata, api_path)
        filename = sanitize_filename(parts[0]) + ".md"
        if category:
            return OUTPUT_DIR / category / filename
        return OUTPUT_DIR / filename

    parent = sanitize_filename(parts[0])
    rest = parts[1:]
    last = sanitize_filename(rest[-1])
    mid = [sanitize_filename(p) for p in rest[:-1]]
    subdir = OUTPUT_DIR / parent
    for m in mid:
        subdir = subdir / m
    return subdir / (last + ".md")


def resolve_output_collision(path, api_path):
    """Ensure distinct pages never share one output file.

    sanitize_filename and case-insensitive filesystems can map two different
    api_paths to the same file; without this the second write silently
    overwrites the first. We disambiguate with a short stable hash of the
    api_path (not a running counter) so the same page always lands on the same
    file across resumes.
    """
    key = str(path).lower()
    with lock:
        owner = allocated_paths.get(key)
        if owner is None or owner == api_path:
            allocated_paths[key] = api_path
            return path
        # First candidate is keyed on a stable hash of api_path so the same page
        # resolves to the same file across resumes. If that candidate is itself
        # already owned by a different page (an 8-hex hash collision, or a real
        # page that happens to carry the "-<hash>" name), keep probing with a
        # counter so we never hand back an already-allocated path.
        base_suffix = hashlib.sha1(api_path.encode("utf-8")).hexdigest()[:8]
        n = 0
        while True:
            suffix = base_suffix if n == 0 else f"{base_suffix}-{n}"
            disambiguated = path.with_name(f"{path.stem}-{suffix}{path.suffix}")
            dkey = str(disambiguated).lower()
            existing = allocated_paths.get(dkey)
            if existing is None or existing == api_path:
                allocated_paths[dkey] = api_path
                return disambiguated
            n += 1


# ---------------------------------------------------------------------------
# Identifier handling — discovers the doc:// prefix from the root page
# ---------------------------------------------------------------------------


def discover_identifier_prefix(data):
    """Extract the doc:// identifier prefix from a page's own identifier field."""
    global DOC_IDENTIFIER_PREFIX
    ident = data.get("identifier", {})
    if isinstance(ident, dict):
        ident = ident.get("url", "")
    if isinstance(ident, str) and ident.startswith("doc://"):
        # e.g. "doc://com.apple.Accessibility/documentation/Accessibility"
        # prefix is "doc://com.apple.Accessibility/documentation/"
        idx = ident.find("/documentation/")
        if idx != -1:
            DOC_IDENTIFIER_PREFIX = ident[: idx + len("/documentation/")]
            return
    # Fallback: scan references for identifiers that match our framework path
    fw_lower = FRAMEWORK_API_PATH.lower() if FRAMEWORK_API_PATH else ""
    for ref_key in data.get("references", {}):
        if not ref_key.startswith("doc://") or "/documentation/" not in ref_key:
            continue
        idx = ref_key.find("/documentation/")
        after = ref_key[idx + len("/documentation/") :]
        if after.lower().startswith(fw_lower + "/") or after.lower() == fw_lower:
            DOC_IDENTIFIER_PREFIX = ref_key[: idx + len("/documentation/")]
            return


def is_same_framework_identifier(ident):
    """Check if an identifier belongs to the framework we're crawling."""
    # The discovered prefix is only bundle-scoped, and several frameworks can
    # share one bundle id (e.g. "doc://com.apple.documentation/..."), so a
    # prefix match alone can let the crawler wander into sibling frameworks
    # through Apple's heavy cross-linking. Always confirm the path segment right
    # after /documentation/ matches the target framework — the same invariant
    # get_output_path/strip_framework_prefix already rely on.
    if not ident.startswith("doc://") or "/documentation/" not in ident:
        return False
    if DOC_IDENTIFIER_PREFIX and not ident.startswith(DOC_IDENTIFIER_PREFIX):
        return False
    idx = ident.find("/documentation/")
    after = ident[idx + len("/documentation/") :].lower()
    fw = (FRAMEWORK_API_PATH or "").lower()
    return bool(fw) and (after == fw or after.startswith(fw + "/"))


def identifier_to_api_path(ident):
    """Convert a doc:// identifier to an API fetch path."""
    if DOC_IDENTIFIER_PREFIX and ident.startswith(DOC_IDENTIFIER_PREFIX):
        return ident[len(DOC_IDENTIFIER_PREFIX) :]
    # Fallback: strip everything up to /documentation/
    idx = ident.find("/documentation/")
    if idx != -1:
        return ident[idx + len("/documentation/") :]
    return ident


def collect_child_identifiers(data):
    children = []
    for section in data.get("topicSections", []):
        for ident in section.get("identifiers", []):
            if is_same_framework_identifier(ident):
                children.append(ident)
    for section in data.get("defaultImplementationsSections", []):
        for ident in section.get("identifiers", []):
            if is_same_framework_identifier(ident):
                children.append(ident)
    return children


# ---------------------------------------------------------------------------
# Crawl engine
# ---------------------------------------------------------------------------


def process_page(api_path):
    global DOC_IDENTIFIER_PREFIX

    data = fetch_json(api_path)
    if not data:
        return None

    # Discover identifier prefix from the first page we fetch
    if DOC_IDENTIFIER_PREFIX is None:
        with lock:
            if DOC_IDENTIFIER_PREFIX is None:
                discover_identifier_prefix(data)

    md = convert_to_markdown(data, api_path)
    out_path = resolve_output_collision(get_output_path(data, api_path), api_path)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(out_path, md)

    metadata = data.get("metadata", {})
    title = metadata.get("title", api_path.split("/")[-1])
    entry = {
        "title": title,
        "file": str(out_path.relative_to(OUTPUT_DIR)),
        "kind": metadata.get("symbolKind", metadata.get("role", "")),
        "roleHeading": metadata.get("roleHeading", ""),
    }

    children = collect_child_identifiers(data)
    child_paths = [identifier_to_api_path(ident) for ident in children]

    # Return the manifest entry instead of recording it here: the crawl driver
    # commits it together with this page's children under a single lock, so a
    # crash can never persist a parent as "done" in the manifest without its
    # children landing in `visited`. Recording it here (before the driver
    # enqueued the children) left a window where an interrupt could mark the
    # parent complete while its subtree was lost on resume.
    return (api_path, entry, child_paths)


def crawl_parallel(start_path, workers, extra_paths=None):
    global page_count

    queue = deque()
    queue.append(start_path)
    visited.add(start_path)

    if extra_paths:
        for p in extra_paths:
            if p not in visited:
                visited.add(p)
                queue.append(p)

    # Manage the executor explicitly rather than via `with`: its __exit__ calls
    # shutdown(wait=True), which on Ctrl-C/SIGTERM blocks until every in-flight
    # request finishes (a retrying page can take ~100s) before main()'s finally
    # can persist state — risking lost progress past a CI/container termination
    # grace period. Returning without waiting lets save_state() run before the
    # at-exit thread join, so progress is flushed promptly.
    executor = ThreadPoolExecutor(max_workers=workers)

    # Keep one in-flight task per worker at all times. A per-batch barrier
    # (submit `workers` pages, then wait for the whole batch before refilling)
    # leaves workers idle whenever a single page is slow — a retrying page can
    # block ~100s on backoff plus timeouts — so we refill continuously as
    # individual futures complete instead.
    in_flight = {}

    def fill():
        while queue and len(in_flight) < workers:
            p = queue.popleft()
            in_flight[executor.submit(process_page, p)] = p

    try:
        fill()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                path = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  ERROR processing {path}: {e}", flush=True)
                    with lock:
                        failed.append({"path": path, "error": str(e)})
                    continue

                if result is None:
                    continue

                api_path_done, entry, child_paths = result

                # Commit the page to the manifest and enqueue its children under
                # one lock so the two are always persisted together. If the
                # manifest entry were written before the children were queued, a
                # crash in between could mark the parent "done" while its subtree
                # was never rediscovered on resume.
                #
                # Dedup on exact-case identifiers (Apple's are canonical); a
                # lower-cased key would merge two case-distinct symbols and drop
                # one, and would break case-sensitive resume re-fetches.
                with lock:
                    page_count += 1
                    manifest[api_path_done] = entry
                    count = page_count
                    for child in child_paths:
                        if child not in visited:
                            visited.add(child)
                            queue.append(child)
                print(f"[{count}] {path}", flush=True)

                if count % 50 == 0:
                    save_state()
                    print(
                        f"  [{count} pages downloaded, ~{len(queue)} in queue]",
                        flush=True,
                    )
            fill()
    finally:
        # Don't block on in-flight (possibly slow/retrying) requests on
        # shutdown; cancel anything still queued so main()'s finally can save
        # state promptly. cancel_futures requires Python 3.9+.
        executor.shutdown(wait=False, cancel_futures=True)


def _atomic_write(path, text):
    # Write to a uniquely-named sibling temp file then os.replace() for an atomic
    # swap, so an interruption mid-write can't truncate/corrupt the output or
    # resume files. The temp name must be unique per call (not a fixed ".tmp"):
    # two workers can resolve to the same target on a case-insensitive
    # filesystem, and a shared temp file would let them clobber each other's
    # bytes before the replace.
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # mkstemp creates the file 0600; widen to 0644 so downloaded docs (and
        # the resume/manifest files) are readable by other users like normal
        # files, instead of owner-only.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state():
    # Snapshot shared state under the lock, then serialize and write to disk
    # outside it. Holding `lock` across two atomic file writes would stall every
    # worker (they all need it to record results and enqueue children) for the
    # full duration of two JSON dumps on each 50-page checkpoint.
    with lock:
        state = {
            "visited": list(visited),
            "failed": list(failed),
            "gone": list(gone),
            "page_count": page_count,
            "framework": FRAMEWORK_API_PATH,
            "identifier_prefix": DOC_IDENTIFIER_PREFIX,
        }
        manifest_snapshot = dict(manifest)
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))
    _atomic_write(MANIFEST_FILE, json.dumps(manifest_snapshot, indent=2, sort_keys=True))


def load_state():
    global visited, failed, page_count, manifest, DOC_IDENTIFIER_PREFIX, gone
    if STATE_FILE.exists():
        try:
            # Read as UTF-8 to match how _atomic_write() writes these files;
            # Path.read_text() would otherwise use the platform's locale
            # encoding and corrupt non-ASCII titles/paths on resume.
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if MANIFEST_FILE.exists():
                manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: state file unreadable ({e}); starting a fresh crawl.")
            return False
        # Don't blend state from a different framework into this run: reusing one
        # --output dir for two frameworks would otherwise merge their visited
        # sets, manifests, and collision maps. The framework is recorded in state
        # by save_state(); compare case-insensitively since the CLI arg's case
        # may differ from the stored canonical name.
        saved_fw = state.get("framework")
        if saved_fw and FRAMEWORK_API_PATH and saved_fw.lower() != FRAMEWORK_API_PATH.lower():
            print(
                f"Warning: existing state is for '{saved_fw}', not '{FRAMEWORK_API_PATH}'; "
                "ignoring stale state and starting a fresh crawl.",
                file=sys.stderr,
            )
            manifest = {}
            return False
        visited = set(state.get("visited", []))
        failed = state.get("failed", [])
        gone = set(state.get("gone", []))
        page_count = state.get("page_count", 0)
        DOC_IDENTIFIER_PREFIX = state.get("identifier_prefix")
        # Pre-register already-written files so a resumed crawl resolves the same
        # collisions the same way it did on the first run.
        for ap, entry in manifest.items():
            rel = entry.get("file")
            if rel:
                allocated_paths[str(OUTPUT_DIR / rel).lower()] = ap
        print(
            f"Resuming: {page_count} pages already downloaded, "
            f"{len(visited)} paths visited"
        )
        return True
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_input(raw):
    """Accept a URL or bare framework name, return the API path (e.g. 'Vision')."""
    raw = raw.strip()
    # Drop any query string or fragment (e.g. "?language=objc", "#topics")
    # before trimming slashes, so they aren't appended to the .json fetch path
    # and turned into guaranteed 404s.
    raw = raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    # Full URL: https://developer.apple.com/documentation/vision
    prefix = "https://developer.apple.com/documentation/"
    if raw.lower().startswith(prefix.lower()):
        return raw[len(prefix) :]
    # Short URL without scheme
    prefix2 = "developer.apple.com/documentation/"
    if raw.lower().startswith(prefix2.lower()):
        return raw[len(prefix2) :]
    # Bare name
    return raw


def main():
    global OUTPUT_DIR, STATE_FILE, MANIFEST_FILE, FRAMEWORK_API_PATH

    parser = argparse.ArgumentParser(
        description="Download Apple developer documentation as markdown."
    )
    parser.add_argument(
        "framework",
        help="Framework name or full URL (e.g. 'Vision' or "
        "'https://developer.apple.com/documentation/accessibility')",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output directory (default: <framework>-docs)",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=12,
        help="Number of parallel download workers (default: 12)",
    )
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be a positive integer")
    # Cap the worker count: more than this yields no throughput gain against
    # Apple's API and mainly risks rate-limiting/connection exhaustion, so reject
    # obvious mistakes (e.g. a stray extra digit) instead of spawning the threads.
    if args.workers > 64:
        parser.error("--workers must be 64 or fewer")

    api_path = parse_input(args.framework)
    if not api_path:
        parser.error("could not determine a framework name from the input")
    FRAMEWORK_API_PATH = api_path

    # Replace path separators so a deep input (e.g. "Vision/VNRequest") yields a
    # single flat output directory instead of an unexpected nested one.
    dir_name = args.output or f"{api_path.lower().replace('/', '-')}-docs"
    OUTPUT_DIR = Path(dir_name)
    STATE_FILE = OUTPUT_DIR / "_state.json"
    MANIFEST_FILE = OUTPUT_DIR / "_manifest.json"

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    resumed = load_state()
    retry_paths = []
    if resumed:
        retry_paths = [f["path"] for f in failed]
        visited.difference_update(retry_paths)
        # Pages are marked visited when queued, not when written, so a crash can
        # leave discovered-but-incomplete pages in `visited` with no manifest
        # entry. Re-enqueue anything visited that never produced a manifest
        # entry, otherwise the resumed crawl silently reports success while
        # missing those pages. Exclude `gone` (known 404s): they legitimately
        # have no manifest entry and must not be re-fetched on every resume.
        pending = [
            p
            for p in visited
            if p not in manifest and p not in gone and p != api_path
        ]
        visited.difference_update(pending)
        retry_paths.extend(pending)
        if retry_paths:
            print(f"Retrying {len(retry_paths)} failed or incomplete paths...")
        failed.clear()
        print("Continuing from previous state...")
    else:
        print(f"Downloading documentation for: {api_path}")

    def _request_shutdown(signum, frame):
        raise KeyboardInterrupt

    # Translate SIGTERM (CI cancellation, container stop, `kill`) into the same
    # KeyboardInterrupt path as Ctrl-C so the try/finally below still runs
    # save_state(); a bare SIGTERM would otherwise terminate the process
    # without flushing progress to disk.
    signal.signal(signal.SIGTERM, _request_shutdown)

    print()
    try:
        crawl_parallel(api_path, args.workers, extra_paths=retry_paths)
    finally:
        # Persist progress even on Ctrl-C or an unexpected error, so a resume
        # recovers everything written so far instead of only the last 50-page
        # checkpoint. Guard the write so a failed checkpoint can't mask the
        # original crawl exception (e.g. KeyboardInterrupt) on its way out.
        try:
            save_state()
        except Exception as e:
            print(f"  WARNING: failed to save final state: {e}", file=sys.stderr)

    print("\n" + "=" * 60)
    print("CRAWL COMPLETE")
    print("=" * 60)
    print(f"Framework: {FRAMEWORK_API_PATH}")
    print(f"Total pages downloaded: {page_count}")
    print(f"Failed requests: {len(failed)}")
    if failed:
        print("Failed pages:")
        for f in failed:
            print(f"  - {f['path']}: {f['error']}")
    print(f"Output directory: {OUTPUT_DIR.resolve()}")
    print(f"Manifest: {MANIFEST_FILE.resolve()}")

    # Signal incomplete crawls to automation: any unrecovered failure, or a run
    # that downloaded nothing, is not a success.
    if failed or page_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
