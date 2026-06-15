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
import json
import os
import re
import sys
import time
import threading
import urllib.request
import urllib.error
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_URL = "https://developer.apple.com/tutorials/data/documentation"
MAX_RETRIES = 3

lock = threading.Lock()
visited = set()
failed = []
manifest = {}
page_count = 0

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
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if retry < MAX_RETRIES:
            wait = 2**retry
            print(f"  HTTP {e.code} for {url}, retrying in {wait}s...")
            time.sleep(wait)
            return fetch_json(path, retry + 1)
        print(f"  FAILED: HTTP {e.code} for {url}")
        with lock:
            failed.append({"path": path, "error": f"HTTP {e.code}"})
        return None
    except Exception as e:
        if retry < MAX_RETRIES:
            wait = 2**retry
            print(f"  Error for {url}: {e}, retrying in {wait}s...")
            time.sleep(wait)
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
            parts.append(f"^{render_inline(item.get('inlineContent', []))}")
        elif t == "subscript":
            parts.append(f"_{render_inline(item.get('inlineContent', []))}")
        elif t == "strikethrough":
            parts.append(f"~~{render_inline(item.get('inlineContent', []))}~~")
        else:
            parts.append(text or render_inline(item.get("inlineContent", [])))
    return "".join(parts)


def render_content_block(block, depth=0):
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
        lang = block.get("syntax", "swift")
        code = "\n".join(block.get("code", []))
        lines.append(f"```{lang}")
        lines.append(code)
        lines.append("```")
        lines.append("")
    elif t == "unorderedList":
        for item in block.get("items", []):
            for i, content in enumerate(item.get("content", [])):
                rendered = render_content_block(content, depth + 1).rstrip()
                if i == 0:
                    lines.append(f"{'  ' * depth}- {rendered}")
                else:
                    lines.append(f"{'  ' * (depth + 1)}{rendered}")
        lines.append("")
    elif t == "orderedList":
        for idx, item in enumerate(block.get("items", []), 1):
            for i, content in enumerate(item.get("content", [])):
                rendered = render_content_block(content, depth + 1).rstrip()
                if i == 0:
                    lines.append(f"{'  ' * depth}{idx}. {rendered}")
                else:
                    lines.append(f"{'  ' * (depth + 1)}{rendered}")
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
        lines.append(f"- **`{name}`**: {desc}")
    lines.append("")
    return "\n".join(lines)


def render_possible_values(section):
    lines = ["## Possible Values", ""]
    for val in section.get("values", []):
        name = val.get("name", "")
        desc = " ".join(render_content_block(c).strip() for c in val.get("content", []))
        if desc:
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
        return encoded[:200].decode("utf-8", "ignore")
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
    if not DOC_IDENTIFIER_PREFIX:
        return ident.startswith("doc://")
    return ident.startswith(DOC_IDENTIFIER_PREFIX)


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
    global page_count, DOC_IDENTIFIER_PREFIX

    data = fetch_json(api_path)
    if not data:
        return None

    # Discover identifier prefix from the first page we fetch
    if DOC_IDENTIFIER_PREFIX is None:
        with lock:
            if DOC_IDENTIFIER_PREFIX is None:
                discover_identifier_prefix(data)

    md = convert_to_markdown(data, api_path)
    out_path = get_output_path(data, api_path)

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

    with lock:
        page_count += 1
        manifest[api_path] = entry
        count = page_count

    return (api_path, child_paths, count)


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

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while queue:
            batch = []
            while queue and len(batch) < workers:
                batch.append(queue.popleft())

            futures = {executor.submit(process_page, path): path for path in batch}

            for future in as_completed(futures):
                path = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    print(f"  ERROR processing {path}: {e}", flush=True)
                    with lock:
                        failed.append({"path": path, "error": str(e)})
                    continue

                if result is None:
                    continue

                _, child_paths, count = result
                print(f"[{count}] {path}", flush=True)

                # Dedup on exact-case identifiers (Apple's are canonical); a
                # lower-cased key would merge two case-distinct symbols and drop
                # one, and would break case-sensitive resume re-fetches.
                with lock:
                    for child in child_paths:
                        if child not in visited:
                            visited.add(child)
                            queue.append(child)

                if count % 50 == 0:
                    with lock:
                        save_state()
                    print(
                        f"  [{count} pages downloaded, ~{len(queue)} in queue]",
                        flush=True,
                    )


def _atomic_write(path, text):
    # Write to a sibling temp file then os.replace() for an atomic swap, so an
    # interruption mid-write can't truncate/corrupt the output or resume files.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_state():
    state = {
        "visited": list(visited),
        "failed": failed,
        "page_count": page_count,
        "framework": FRAMEWORK_API_PATH,
        "identifier_prefix": DOC_IDENTIFIER_PREFIX,
    }
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))
    _atomic_write(MANIFEST_FILE, json.dumps(manifest, indent=2, sort_keys=True))


def load_state():
    global visited, failed, page_count, manifest, DOC_IDENTIFIER_PREFIX
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if MANIFEST_FILE.exists():
                manifest = json.loads(MANIFEST_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: state file unreadable ({e}); starting a fresh crawl.")
            return False
        visited = set(state.get("visited", []))
        failed = state.get("failed", [])
        page_count = state.get("page_count", 0)
        DOC_IDENTIFIER_PREFIX = state.get("identifier_prefix")
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
    raw = raw.strip().rstrip("/")
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

    api_path = parse_input(args.framework)
    FRAMEWORK_API_PATH = api_path

    dir_name = args.output or f"{api_path.lower()}-docs"
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
        # missing those pages.
        pending = [p for p in visited if p not in manifest and p != api_path]
        visited.difference_update(pending)
        retry_paths.extend(pending)
        if retry_paths:
            print(f"Retrying {len(retry_paths)} failed or incomplete paths...")
        failed.clear()
        print("Continuing from previous state...")
    else:
        print(f"Downloading documentation for: {api_path}")

    print()
    crawl_parallel(api_path, args.workers, extra_paths=retry_paths)
    save_state()

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
