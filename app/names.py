"""Album names: album-names.tsv (in the dashboard's data folder).

One line per album, UTF-8, tab-separated, with a header:
    set_id  folder  gallerydl_title  facebook_title  custom_name
Only `custom_name` is meant to be edited (here or by hand); the other columns
are refreshed from the disk. Tabs, newlines and backslashes inside values are
escaped as \\t, \\n and \\\\.

The custom name is applied to Immich through immich-folder-album-creator's
`.albumprops` files (override_name) by tools/apply_albumprops.py, so that no
folder ever has to be renamed (an Immich external asset is identified by its
path: renaming a folder would create new assets and lose faces/albums).
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from dataclasses import dataclass
from typing import Optional

COLUMNS = ("set_id", "folder", "gallerydl_title", "facebook_title", "custom_name")
MAX_NAME = 200
MARKER = "# managed by gallery-dl-dashboard"
RE_OVERRIDE = re.compile(r'^\s*override_name\s*:\s*(.+?)\s*$', re.M)


def _esc(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "")


def _unesc(v: str) -> str:
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            n = v[i + 1]
            out.append({"t": "\t", "n": "\n", "\\": "\\"}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def clean_name(name: str) -> str:
    """Normalize a custom album name; raises ValueError when unusable."""
    name = unicodedata.normalize("NFC", name or "")
    # drop real control characters only: keep format characters (emoji joiners,
    # right-to-left marks) that non-latin names need
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cs"))
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > MAX_NAME:
        raise ValueError(f"name longer than {MAX_NAME} characters")
    return name


@dataclass
class NameRow:
    set_id: str
    folder: str = ""
    gallerydl_title: str = ""
    facebook_title: str = ""
    custom_name: str = ""

    @property
    def effective(self) -> str:
        return self.custom_name or self.folder_title

    @property
    def folder_title(self) -> str:
        """Album name the creator tool derives from the folder (no override)."""
        return re.sub(r"\s*\([^()]+\)\s*$", "", self.folder) if self.folder else self.gallerydl_title


def album_names(rows, paths: dict) -> dict:
    """{set_id: (name, source)}: the name tools/apply_albumprops.py writes in
    each album's .albumprops (keep both in sync). rows: NameRow list; paths:
    {set_id: album folder path}. source: custom | facebook | folder, plus
    "+account" / "+id" when a duplicate name had to be told apart."""
    out = {}
    for r in rows:
        f = paths.get(r.set_id)
        if not f:
            continue
        try:
            fb = clean_name(r.facebook_title)
        except ValueError:
            fb = clean_name(r.facebook_title[:MAX_NAME])
        if r.custom_name:
            out[r.set_id] = [r.custom_name, "custom", f]
        elif fb:
            out[r.set_id] = [fb, "facebook", f]
        else:
            base = re.sub(r"\s*\([^()]+\)\s*$", "", os.path.basename(f))
            out[r.set_id] = [" ".join(base.split()) or r.set_id, "folder", f]
    for _ in range(2):
        by = {}
        for sid, (n, src, f) in out.items():
            by.setdefault(n.casefold(), []).append(sid)
        for sids in by.values():
            if len(sids) < 2:
                continue
            accounts = {os.path.basename(os.path.dirname(out[s][2])) for s in sids}
            for s in sids:
                n, src, f = out[s]
                if len(accounts) == len(sids) and "+account" not in src:
                    out[s] = [f"{n} ({os.path.basename(os.path.dirname(f))})", src + "+account", f]
                elif "+id" not in src:
                    out[s] = [f"{n} ({s})", src + "+id", f]
    return {s: (n, src) for s, (n, src, _) in out.items()}


class NamesFile:
    def __init__(self, path: Optional[str]):
        self.path = path
        self.lock = threading.Lock()
        self.rows: dict[str, NameRow] = {}
        self.mtime: Optional[float] = None
        self.error: Optional[str] = None

    # ---------------------------------------------------------------- io
    def load(self):
        with self.lock:
            self._load()

    def _load(self):
        self.error = None
        if not self.path or not os.path.exists(self.path):
            self.rows, self.mtime = {}, None
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.mtime = os.stat(self.path).st_mtime
        except OSError as e:
            self.error = f"cannot read {self.path}: {e.__class__.__name__}"
            return
        rows = {}
        header = lines[0].split("\t") if lines else []
        idx = {c: header.index(c) for c in COLUMNS if c in header}
        if "set_id" not in idx:
            self.error = "album-names.tsv has no header line with 'set_id'"
            return
        for n, line in enumerate(lines[1:], 2):
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            get = lambda c: _unesc(parts[idx[c]]) if c in idx and idx[c] < len(parts) else ""  # noqa: E731
            sid = get("set_id").strip()
            if not sid:
                continue
            try:
                custom = clean_name(get("custom_name"))
            except ValueError as e:
                self.error = f"line {n}: {e}"
                custom = ""
            rows[sid] = NameRow(sid, get("folder"), get("gallerydl_title"),
                                get("facebook_title"), custom)
        self.rows = rows

    def _write(self):
        if not self.path:
            raise OSError("no writable data folder: album names cannot be saved")
        tmp = f"{self.path}.tmp"
        body = "\t".join(COLUMNS) + "\n" + "".join(
            "\t".join(_esc(getattr(r, c)) for c in COLUMNS) + "\n"
            for r in sorted(self.rows.values(), key=lambda r: (r.folder.lower(), r.set_id)))
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(self.path):
            try:
                os.replace(self.path, self.path + ".bak")
            except OSError:
                pass
        os.replace(tmp, self.path)
        self.mtime = os.stat(self.path).st_mtime

    def _reload_if_changed(self):
        """Pick up edits made by hand since we last read the file."""
        if self.path and os.path.exists(self.path):
            if self.mtime is None or os.stat(self.path).st_mtime != self.mtime:
                self._load()

    # ---------------------------------------------------------------- api
    def sync(self, albums: dict, write: bool = False, fb_titles: Optional[dict] = None) -> list[NameRow]:
        """Merge disk albums (set_id -> (folder, gallerydl_title)) and Facebook
        titles (from imported pages) into the rows. Custom names are never
        touched here."""
        with self.lock:
            self._reload_if_changed()
            changed = False
            for sid, t in (fb_titles or {}).items():
                r = self.rows.get(sid)
                if r is not None and t and r.facebook_title != t:
                    r.facebook_title = t
                    changed = True
            for sid, (folder, title) in albums.items():
                r = self.rows.get(sid)
                if r is None:
                    self.rows[sid] = NameRow(sid, folder or "", title or "",
                                             (fb_titles or {}).get(sid) or "")
                    changed = True
                elif (folder and r.folder != folder) or (title and r.gallerydl_title != title):
                    r.folder = folder or r.folder
                    r.gallerydl_title = title or r.gallerydl_title
                    changed = True
            if write and self.path and (changed or not os.path.exists(self.path)):
                self._write()
            return sorted(self.rows.values(), key=lambda r: (r.folder.lower(), r.set_id))

    def set_custom(self, set_id: str, name: str) -> NameRow:
        with self.lock:
            self._reload_if_changed()            # never clobber a hand edit
            r = self.rows.get(set_id)
            if r is None:
                raise KeyError(set_id)
            r.custom_name = clean_name(name)
            self._write()
            return r

    def fill_from_facebook(self) -> int:
        """Empty custom names take the Facebook title (when it differs from
        the folder's). Names already chosen are never overwritten."""
        with self.lock:
            self._reload_if_changed()
            n = 0
            for r in self.rows.values():
                if not r.custom_name and r.facebook_title:
                    try:
                        t = clean_name(r.facebook_title)
                    except ValueError:
                        continue
                    if t and t != r.folder_title:
                        r.custom_name = t
                        n += 1
            if n:
                self._write()
            return n

    def duplicates(self) -> dict[str, list[str]]:
        """Effective names used by several albums (the creator tool would merge them)."""
        by = {}
        for r in self.rows.values():
            if r.custom_name:
                by.setdefault(r.custom_name.casefold(), []).append(r.set_id)
        return {k: v for k, v in by.items() if len(v) > 1}


# ------------------------------------------------------------------- .albumprops
def albumprops_content(name: str) -> str:
    import json
    # a JSON string is a valid YAML double-quoted scalar
    return (f"{MARKER} (tools/apply_albumprops.py): album name for immich-folder-album-creator\n"
            f"override_name: {json.dumps(name, ensure_ascii=False)}\n")


def read_albumprops(folder_path: str) -> tuple[str, Optional[str]]:
    """-> (state, override_name). state: none | ours | foreign | unreadable"""
    p = os.path.join(folder_path, ".albumprops")
    if not os.path.exists(p):
        return "none", None
    try:
        with open(p, encoding="utf-8") as fh:
            txt = fh.read()
    except (OSError, UnicodeDecodeError):
        return "unreadable", None
    m = RE_OVERRIDE.search(txt)
    val = None
    if m:
        raw = m.group(1)
        try:
            import json
            val = json.loads(raw) if raw.startswith('"') else raw.strip("'")
        except ValueError:
            val = raw
    return ("ours" if txt.startswith(MARKER) else "foreign"), val
