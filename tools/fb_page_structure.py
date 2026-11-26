#!/usr/bin/env python3
"""Print the STRUCTURE of a saved Facebook page, never its content.

Used to learn where Facebook puts album titles and photo counts, without
sharing any personal data. Output contains only:
  * JSON key paths (array indices collapsed to [], keys that look like IDs
    replaced by <id>) with value TYPES; numbers only as a size class
    (int<100, int<10k, int<1M, int>=1M), strings only as str/str-empty;
    the values of a few schema keys (__typename, __isMedia...) which are
    Facebook class names, not user data;
  * visible-text PATTERNS: digits -> N, every word -> w, except a short list
    of interface words (photos, items, éléments...);
  * link PATTERNS (path + parameter names, numbers -> N).
No title, name, caption, ID, URL or text is printed. Read the output before
sharing it anyway.

Usage:  python3 fb_page_structure.py PAGE.html [PAGE2.html ...] > structure.txt
Standard library only.
"""
import html.parser
import json
import re
import sys
from collections import Counter, defaultdict

SCHEMA_KEYS = {"__typename", "__isNode", "__isMedia", "__isProfile", "__isEntity",
               "__isActor", "__module_component", "__dr"}
UI_WORDS = {
    # en
    "photo", "photos", "video", "videos", "item", "items", "album", "albums",
    "post", "posts", "of", "and", "more", "see", "all", "shared", "with", "public",
    "friends", "only", "me", "custom", "updated", "added", "new", "by", "contributors",
    # fr
    "élément", "éléments", "vidéo", "vidéos", "publication", "publications", "de", "et",
    "plus", "voir", "tout", "tous", "partagé", "avec", "public", "amis", "moi",
    "uniquement", "mis", "à", "jour", "ajouté", "ajoutées", "nouveau", "par",
    "contributeurs", "il", "y", "a", "min", "h", "j", "sem", "an", "ans",
}
FB_PATH_WORDS = {"media", "set", "photo", "photo.php", "photos", "photos_albums", "photos_by",
                 "photos_of", "posts", "permalink.php", "profile.php", "groups", "people", "watch",
                 "videos", "story.php", "albums", "pages", "events", "reel", "stories", "login",
                 "friends", "about", "help", "settings", "notifications", "messages", "marketplace",
                 "gaming", "bookmarks", "search", "hashtag", "privacy", "policies", "home.php"}
KEY_WORDS = re.compile(r"title|count|total|media|album|set|name|item|photo|edge|node|"
                       r"collection|token|url|page_info|has_next|cursor|subtitle|text", re.I)


def size_class(n):
    n = abs(n)
    return "int<100" if n < 100 else "int<10k" if n < 10_000 else "int<1M" if n < 1_000_000 else "int>=1M"


def mask_key(k):
    if re.fullmatch(r"[\d:._-]+", k) or re.fullmatch(r"[0-9a-f]{12,}", k, re.I) or len(k) > 40 or " " in k:
        return "<id>"
    return k


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.texts, self.links = [], [], Counter()
        self._in_script = None
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._in_script = a.get("type", "")
            self._buf = []
        elif tag in ("style", "noscript"):
            self._skip += 1
        if tag == "a" and a.get("href"):
            self.links[link_pattern(a["href"])] += 1

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script is not None:
            self.scripts.append((self._in_script, "".join(self._buf)))
            self._in_script = None
        elif tag in ("style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_script is not None:
            self._buf.append(data)
        elif not self._skip and data.strip():
            self.texts.append(data.strip())


def link_pattern(href):
    m = re.match(r"(?:https?://[^/]+)?(/[^?#]*)?(?:\?([^#]*))?", href)
    path = (m.group(1) or "") if m else ""
    path = re.sub(r"\d+", "N", path)
    # only known Facebook path words are kept: any other segment may be a username
    path = "/".join(seg if (seg in FB_PATH_WORDS or seg in ("", "N")) else "<seg>" for seg in path.split("/"))
    params = sorted({p.split("=")[0] for p in (m.group(2) or "").split("&") if p}) if m else []
    sets = ""
    if m and m.group(2):
        q = dict(p.split("=", 1) for p in m.group(2).split("&") if "=" in p)
        if "set" in q:
            sets = " set=" + re.sub(r"[0-9]+", "N", q["set"].split(".")[0]) + ".N"
    return f"{path}?{'&'.join(params)}{sets}" if params else path


def text_pattern(t):
    out = []
    for tok in re.findall(r"\d+(?:[.,\s]\d+)*|\w+|[^\w\s]", t):
        if tok[0].isdigit():
            out.append("N")
        elif tok.lower() in UI_WORDS:
            out.append(tok.lower())
        elif re.fullmatch(r"\w+", tok):
            if out and out[-1] in ("w", "w…"):
                out[-1] = "w…"
            else:
                out.append("w")
        else:
            out.append(tok if tok in "·•|/()-:,.+" else "?")
    return " ".join(out)[:80]


def walk(o, path, paths, schema):
    if isinstance(o, dict):
        for k, v in o.items():
            mk = mask_key(k)
            if k in SCHEMA_KEYS and isinstance(v, str):
                schema[f"{path}.{k}"][v] += 1
            walk(v, f"{path}.{mk}" if path else mk, paths, schema)
    elif isinstance(o, list):
        for v in o:
            walk(v, path + "[]", paths, schema)
    else:
        if isinstance(o, bool):
            t = "bool"
        elif isinstance(o, int):
            t = size_class(o)
        elif isinstance(o, float):
            t = "float"
        elif isinstance(o, str):
            t = "str-empty" if not o else ("str-digits" if o.isdigit() else "str")
        else:
            t = "null"
        paths[path][t] += 1


def main(files):
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        p = Page()
        p.feed(raw)
        paths, schema = defaultdict(Counter), defaultdict(Counter)
        blobs = 0
        for typ, body in p.scripts:
            body = body.strip()
            if not body or body[0] not in "{[":
                continue
            try:
                walk(json.loads(body), "", paths, schema)
                blobs += 1
            except ValueError:
                continue
        print(f"===== file {files.index(f) + 1}: {len(raw)} bytes, {len(p.scripts)} scripts, "
              f"{blobs} JSON blobs, {len(p.texts)} text nodes")
        print("\n--- JSON paths with interesting key names (count × path : types)")
        interesting = [(pth, c) for pth, c in paths.items() if KEY_WORDS.search(pth.rsplit(".", 2)[-1] if "." in pth else pth)]
        for pth, c in sorted(interesting, key=lambda x: -sum(x[1].values()))[:250]:
            print(f"{sum(c.values()):6d} × {pth[-160:]} : {', '.join(f'{t}×{n}' for t, n in c.most_common())}")
        print("\n--- schema names (__typename ...)")
        for pth, c in sorted(schema.items(), key=lambda x: -sum(x[1].values()))[:80]:
            print(f"{pth[-120:]} : {', '.join(f'{v}×{n}' for v, n in c.most_common(6))}")
        print("\n--- visible text patterns containing a number (count × pattern)")
        tp = Counter(text_pattern(t) for t in p.texts)
        for pat, n in tp.most_common():
            if "N" in pat.split():
                print(f"{n:6d} × {pat}")
        print("\n--- link patterns (count × pattern)")
        for pat, n in p.links.most_common(40):
            print(f"{n:6d} × {pat}")
        print("\n--- raw markers")
        for marker in ("mediaSetToken", "TimelineAppCollectionAlbumsRenderer", "pageItems",
                       '"page_info"', "has_next_page", "media_count", "photo_count",
                       "item_count", '"count":', "set=a."):
            print(f"{raw.count(marker):6d} × {marker}")
        print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
