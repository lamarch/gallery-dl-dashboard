"""Read a saved Facebook "albums" page (Web Page, Complete, after scrolling).

What the page contains (learned with tools/fb_page_structure.py):
  * the rendered album grid: one link per album to /media/set/?set=a.<id>,
    with the album title and a count text "<N> éléments" / "<N> élément"
    (English: "<N> items" / "<N> item"; other words are accepted too);
  * embedded JSON for the first albums only (the rest is loaded while
    scrolling): ...collection.pageItems.edges[].node with title.text,
    subtitle_text.text (the count text) and url.
Both are read; the grid gives every album, the JSON confirms the first ones.

Only album IDs, titles and counts leave this module; the page itself is
never stored (it can contain session tokens).
"""
from __future__ import annotations

import html.parser
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

# "276 éléments", "1 234 éléments" (thin/no-break spaces), "1 élément", "12 items"
RE_COUNT = re.compile(r"^\s*(\d[\d\s  .,]*)\s*([kKmM])?\s*"
                      r"(éléments?|elements?|items?|photos?|vidéos?|videos?)\s*$", re.I)


def parse_count(text: str):
    """-> (count, approximate) or None."""
    m = RE_COUNT.match(text or "")
    if not m:
        return None
    num, suffix = m.group(1), m.group(2)
    if suffix:                                   # "1,2 k": rounded by Facebook
        try:
            v = float(re.sub(r"[\s  ]", "", num).replace(",", "."))
        except ValueError:
            return None
        return int(round(v * (1000 if suffix.lower() == "k" else 1_000_000))), True
    digits = re.sub(r"\D", "", num)
    return (int(digits), False) if digits else None


def set_id_of(href: str) -> Optional[str]:
    try:
        u = urlsplit(href)
    except ValueError:
        return None
    if "/media/set" not in u.path:
        return None
    sid = (parse_qs(u.query).get("set") or [""])[0]
    return unquote(sid) if re.fullmatch(r"a\.\d+", unquote(sid)) else None


@dataclass
class FbAlbum:
    set_id: str
    title: Optional[str] = None
    count: Optional[int] = None
    approximate: bool = False
    source: str = "grid"         # grid | json | grid+json


@dataclass
class FbPage:
    page_title: Optional[str] = None   # e.g. "<name> | Facebook"
    albums: dict = field(default_factory=dict)   # set_id -> FbAlbum (page order)
    json_albums: int = 0
    warnings: list = field(default_factory=list)


class _Grid(html.parser.HTMLParser):
    """Walks the rendered page: album links, texts inside/after them."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.order: list = []              # set_ids in page order
        self.titles: dict = {}
        self.counts: dict = {}
        self.page_title = None
        self._in_title = False
        self._anchor_depth = 0             # >0 while inside an album link
        self._anchor_sid = None
        self._last_sid = None              # last album link seen (for texts after it)
        self._skip = 0
        self._scripts = []
        self._in_script = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "noscript"):
            self._skip += 1
        if tag == "script":
            self._in_script = True
            self._buf = []
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            sid = set_id_of(dict(attrs).get("href") or "")
            if sid:
                self._anchor_depth, self._anchor_sid, self._last_sid = 1, sid, sid
                if sid not in self.titles:
                    self.order.append(sid)
                    self.titles[sid] = None
                return
            if self._anchor_depth:
                self._anchor_depth += 1
        elif self._anchor_depth and tag not in ("img", "br", "input", "meta", "link"):
            self._anchor_depth += 1

    def handle_endtag(self, tag):
        if tag in ("style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "script" and self._in_script:
            self._scripts.append("".join(self._buf))
            self._in_script = False
            return
        if tag == "title":
            self._in_title = False
        if self._anchor_depth and tag not in ("img", "br", "input", "meta", "link"):
            self._anchor_depth -= 1
            if self._anchor_depth == 0:
                self._anchor_sid = None

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)
            return
        if self._skip:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title and self.page_title is None:
            self.page_title = text
            return
        sid = self._anchor_sid or self._last_sid
        if sid is None:
            return
        c = parse_count(text)
        if c is not None:
            if sid not in self.counts:
                self.counts[sid] = c
            return
        # first other text inside the link (or right after it) is the title
        if self.titles.get(sid) is None and sid not in self.counts:
            self.titles[sid] = text


def _walk_json(o, out):
    """Find album nodes: dicts with title.text + url to a media set."""
    if isinstance(o, dict):
        url = o.get("url")
        title = o.get("title")
        if isinstance(url, str) and isinstance(title, dict) and "text" in title:
            sid = set_id_of(url)
            if sid:
                sub = o.get("subtitle_text")
                out.append((sid, title.get("text"),
                            sub.get("text") if isinstance(sub, dict) else None))
        for v in o.values():
            _walk_json(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk_json(v, out)


def parse_albums_page(raw: str) -> FbPage:
    g = _Grid()
    g.feed(raw)
    page = FbPage(page_title=g.page_title)
    for sid in g.order:
        c = g.counts.get(sid)
        page.albums[sid] = FbAlbum(sid, g.titles.get(sid), c[0] if c else None,
                                   bool(c and c[1]))
    found = []
    for body in g._scripts:
        body = body.strip()
        if body[:1] in "{[":
            try:
                _walk_json(json.loads(body), found)
            except ValueError:
                continue
    seen = set()
    for sid, title, sub in found:
        if sid in seen:
            continue
        seen.add(sid)
        page.json_albums += 1
        c = parse_count(sub or "")
        a = page.albums.get(sid)
        if a is None:
            page.albums[sid] = FbAlbum(sid, title, c[0] if c else None, bool(c and c[1]), "json")
            continue
        a.source = "grid+json"
        if title and a.title != title:
            a.title = title                      # the JSON title is exact
        if c and a.count is not None and c[0] != a.count:
            page.warnings.append(f"{sid}: count differs between grid ({a.count}) and data ({c[0]})")
        if a.count is None and c:
            a.count, a.approximate = c
    missing_counts = [s for s, a in page.albums.items() if a.count is None]
    if missing_counts:
        page.warnings.append(f"{len(missing_counts)} album(s) without a photo count")
    if not page.albums:
        page.warnings.append("no album found: save the albums page as “Web Page, Complete” "
                             "after scrolling to the bottom (“HTML only” has no albums)")
    return page
