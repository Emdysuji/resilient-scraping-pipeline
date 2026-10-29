"""
inject_component.py
--------------------
Splices a self-contained component (component.html) into a client page
(client_page.html) at a location described by placement.json.

Designed to never crash and to always produce an output file, even when the
placement data is imperfect or the client HTML is messy. It tries several
increasingly forgiving strategies in order and stops at the first one that
actually works:

  1. CSS selector match          (placement.selector, or per-candidate)
  2. Anchor tag/attribute match  (parse the literal anchor HTML, find an
                                   element in the client page with the same
                                   tag + id/class/attrs)
  3. Raw substring splice        (find the literal anchor text in the raw
                                   HTML string and splice around it)
  4. Last-resort fallback        (insert just before </body>, or at the very
                                   end of the file if there's no </body>)

placement.json is expected to look like (see master_prompt_fixed.txt):

{
  "placement": {
    "position": "after",
    "candidates": [
      {"anchor": "<section id=\"services\">", "selector": "#services"},
      {"anchor": "<h2>Our Services</h2>"}
    ],
    "instruction": "..."
  }
}

Older single-anchor JSON (with top-level "selector" and
placement.anchor) is also accepted for backwards compatibility.
"""

import json
import logging
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    from bs4.element import Tag
except ImportError:
    print("This script requires beautifulsoup4. Install it with:")
    print("    pip install beautifulsoup4")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("inject_component")

CLIENT_FILE = "client_page.html"
COMPONENT_FILE = "component.html"
PLACEMENT_FILE = "placement.json"
OUTPUT_FILE = "index.html"


# ---------------------------------------------------------------- helpers --

def safe_read(path: str) -> str | None:
    """Read a file, trying a couple of encodings. Never raises."""
    p = Path(path)
    if not p.exists():
        log.warning("File not found: %s", path)
        return None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, OSError) as e:
            log.debug("Failed reading %s as %s: %s", path, enc, e)
    log.warning("Could not decode %s with any known encoding.", path)
    return None


def safe_load_json(path: str) -> dict:
    """Load placement JSON. Returns {} (never raises) on any problem."""
    raw = safe_read(path)
    if raw is None:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("Invalid JSON in %s (%s). Proceeding with no placement "
                    "data; will fall back to end-of-body insertion.", path, e)
        return {}


def parse_html(html: str):
    """Parse HTML, preferring lxml for resilience, falling back to the
    stdlib parser if lxml isn't installed. Never raises."""
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception as e:  # pragma: no cover - defensive
            log.debug("Parser %s failed: %s", parser, e)
    # Absolute last resort: html.parser again, letting bs4 do its best.
    return BeautifulSoup(html, "html.parser")


def normalize_candidates(placement_data: dict) -> list[dict]:
    """
    Accepts either the new multi-candidate schema or the older single-anchor
    schema and returns a flat list of candidate dicts:
        [{"anchor": str|None, "selector": str|None}, ...]
    Always returns at least an empty list (never raises).
    """
    placement = placement_data.get("placement", {}) or {}
    candidates = []

    for c in placement.get("candidates", []) or []:
        if isinstance(c, dict):
            candidates.append({
                "anchor": c.get("anchor"),
                "selector": c.get("selector"),
            })

    # Backwards compatibility: old flat shape.
    legacy_anchor = placement.get("anchor")
    legacy_selector = placement_data.get("selector") or placement.get("selector")
    if legacy_anchor or legacy_selector:
        candidates.append({"anchor": legacy_anchor, "selector": legacy_selector})

    return candidates


def get_position(placement_data: dict) -> str:
    position = (
        placement_data.get("placement", {}).get("position")
        or placement_data.get("position")
        or "after"
    )
    position = str(position).strip().lower()
    if position not in ("before", "after", "append", "prepend"):
        log.warning("Unknown position '%s'; defaulting to 'after'.", position)
        position = "after"
    return position


def anchor_to_tag_query(anchor_html: str):
    """
    Parse a literal anchor HTML snippet and return (tag_name, attrs) so we
    can search for a matching element in the real client soup, even if the
    anchor snippet itself doesn't appear verbatim (e.g. attribute order
    differs, or the file was minified/reformatted).
    """
    # html.parser is used on purpose: lxml wraps every fragment in an implicit
    # <html><body>, so find(True) would always return <html> and the search
    # would match the page root instead of the element the anchor describes.
    frag = BeautifulSoup(anchor_html, "html.parser")
    tag = frag.find(True)  # first real tag in the fragment
    if tag is None or not isinstance(tag, Tag):
        return None, {}
    attrs = {k: v for k, v in tag.attrs.items() if k in ("id", "class")}
    return tag.name, attrs


def find_target_by_selector(soup, selector: str | None):
    if not selector:
        return None
    try:
        return soup.select_one(selector)
    except Exception as e:
        log.debug("Selector '%s' raised %s", selector, e)
        return None


def find_target_by_anchor_tag(soup, anchor_html: str | None):
    if not anchor_html:
        return None
    tag_name, attrs = anchor_to_tag_query(anchor_html)
    if not tag_name:
        return None
    try:
        if attrs.get("class"):
            # bs4 wants class as a list for exact multi-class matches; try
            # exact first, then "contains any of these classes".
            match = soup.find(tag_name, attrs=attrs)
            if match:
                return match
            classes = attrs["class"] if isinstance(attrs["class"], list) else [attrs["class"]]
            return soup.find(tag_name, class_=lambda c: c and any(cl in c for cl in classes))
        if attrs.get("id"):
            return soup.find(tag_name, id=attrs["id"])
        return soup.find(tag_name)
    except Exception as e:
        log.debug("Tag/attr search for <%s %s> raised %s", tag_name, attrs, e)
        return None


def try_raw_string_splice(client_html: str, anchor_html: str | None,
                           component_html: str, position: str) -> str | None:
    """
    Last structural fallback before giving up entirely: locate the literal
    anchor text inside the raw HTML string and splice the component in
    relative to it. Works even on HTML that BeautifulSoup mangles when
    re-serialized (rare, but possible with broken markup).
    """
    if not anchor_html:
        return None
    anchor_stripped = anchor_html.strip()
    idx = client_html.find(anchor_stripped)
    if idx == -1:
        return None

    if position == "before":
        return client_html[:idx] + component_html + "\n" + client_html[idx:]

    # after / append / prepend all treated as "after the anchor text" here,
    # since we don't have a parsed element to append/prepend inside of.
    end = idx + len(anchor_stripped)
    return client_html[:end] + "\n" + component_html + client_html[end:]


def insert_at_end_of_body(client_html: str, component_html: str) -> str:
    """Absolute last resort: guarantees the component always ends up
    somewhere in the output, even with zero usable placement data."""
    lower = client_html.lower()
    body_close = lower.rfind("</body>")
    if body_close != -1:
        return client_html[:body_close] + component_html + "\n" + client_html[body_close:]
    log.warning("No </body> tag found; appending component to end of file.")
    return client_html + "\n" + component_html


def component_nodes(component_soup) -> list:
    """
    Return the component's real top-level nodes.

    When a fragment is parsed with lxml it gets wrapped in an implicit
    <html><head>/<body> structure (a leading <style> or <script> is even moved
    into <head>). Inserting that wrapper would paste stray <html>/<body> tags
    into the client page, so unwrap it and keep only the actual content.
    """
    html_tag = component_soup.find("html")
    if html_tag is None:  # html.parser does not add a wrapper
        return list(component_soup.contents)
    nodes = []
    for part in ("head", "body"):
        element = html_tag.find(part, recursive=False)
        if element is not None:
            nodes.extend(list(element.contents))
    return nodes


def insert_via_soup(soup, target: Tag, component_soup, position: str) -> bool:
    """Insert the component's top-level nodes relative to `target` inside a
    parsed soup. Returns True on success."""
    contents = component_nodes(component_soup)
    if not contents:
        log.warning("Component HTML is empty; nothing to insert.")
        return False
    try:
        if position == "before":
            for item in contents:
                target.insert_before(item)
        elif position == "append":
            for item in contents:
                target.append(item)
        elif position == "prepend":
            for item in reversed(contents):
                target.insert(0, item)
        else:  # "after"
            for item in reversed(contents):
                target.insert_after(item)
        return True
    except Exception as e:
        log.debug("Soup-based insertion failed: %s", e)
        return False


# ------------------------------------------------------------------- main --

def inject_component() -> bool:
    client_html = safe_read(CLIENT_FILE)
    component_html = safe_read(COMPONENT_FILE)
    placement_data = safe_load_json(PLACEMENT_FILE)

    if client_html is None:
        log.error("Cannot proceed without %s.", CLIENT_FILE)
        return False
    if component_html is None:
        log.warning("No component found at %s; writing client page "
                    "unchanged to %s.", COMPONENT_FILE, OUTPUT_FILE)
        component_html = ""

    candidates = normalize_candidates(placement_data)
    position = get_position(placement_data)

    soup = parse_html(client_html)
    component_soup = parse_html(component_html)

    used_strategy = None
    result_html = None

    # Strategy 1 & 2: try each candidate, selector first then anchor-tag match.
    for i, candidate in enumerate(candidates, start=1):
        target = find_target_by_selector(soup, candidate.get("selector"))
        strategy = f"selector (candidate {i})"
        if target is None:
            target = find_target_by_anchor_tag(soup, candidate.get("anchor"))
            strategy = f"anchor tag/attrs (candidate {i})"
        if target is not None:
            if insert_via_soup(soup, target, component_soup, position):
                used_strategy = strategy
                result_html = str(soup)
                break
            log.debug("Found target for candidate %d but insertion failed; "
                      "trying next strategy.", i)

    # Strategy 3: raw string splice, tried across all candidates.
    if result_html is None:
        for i, candidate in enumerate(candidates, start=1):
            spliced = try_raw_string_splice(
                client_html, candidate.get("anchor"), component_html, position
            )
            if spliced is not None:
                used_strategy = f"raw string splice (candidate {i})"
                result_html = spliced
                break

    # Strategy 4: guaranteed fallback.
    if result_html is None:
        used_strategy = "fallback: end of <body>"
        result_html = insert_at_end_of_body(client_html, component_html)

    try:
        Path(OUTPUT_FILE).write_text(result_html, encoding="utf-8")
    except OSError as e:
        log.error("Could not write %s: %s", OUTPUT_FILE, e)
        return False

    if used_strategy.startswith("fallback"):
        log.warning(
            "No placement candidate matched the client HTML. Used %s. "
            "Check placement.json against the actual client page.",
            used_strategy,
        )
    else:
        log.info("Success using strategy: %s", used_strategy)
    log.info("Wrote %s", OUTPUT_FILE)
    return True


if __name__ == "__main__":
    inject_component()