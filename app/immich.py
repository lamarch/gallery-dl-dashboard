"""Read-only check of the Immich side: is every downloaded album in Immich,
under the expected name, with all its photos?

Only GET /api/albums, GET /api/assets/<id> (one album cover, to find where the
downloads live in Immich) and POST /api/search/metadata (a search: reads
assets, changes nothing) are called. Nothing is ever written to Immich. What is
fetched stays in memory; nothing is stored on disk.

The API key is read from a file (IMMICH_API_KEY_FILE, default
/run/secrets/immich_api_key) that other users cannot read; never from an
environment variable (those show in `docker inspect`). It needs the
album.read and asset.read permissions.
"""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

SID_DIR = re.compile(r"\(([^()]+)\)\s*$")
SET_IN_DESC = re.compile(r"[?&]set=([^&\s\"'<>]+)")
RE_ID = re.compile(r"_(\d+)\.[^.]+$")
PAGE = 1000


def _norm(v) -> str:
    return " ".join((v or "").casefold().split())


class ImmichError(Exception):
    pass


def read_key(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError as e:
        raise ImmichError(f"cannot read the API key file {path}: {e.strerror}") from None
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ImmichError(f"the API key file {path} can be read by other users: chmod 600 it on the host")
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ImmichError(f"cannot read the API key file {path}: {e.__class__.__name__}") from None
    if not key or any(c.isspace() for c in key):
        raise ImmichError(f"the API key file {path} must contain the key only")
    return key


class Api:
    def __init__(self, url: str, key: str, timeout: float = 60):
        self.base = url.rstrip("/") + "/api"
        self.key = key
        self.timeout = timeout

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "x-api-key": self.key, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise ImmichHTTP(e.code, path) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ImmichError(f"cannot reach Immich: {reason}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise ImmichError(f"Immich answered something that is not JSON on {path}") from None


class ImmichHTTP(ImmichError):
    def __init__(self, code, path):
        hint = {401: "the API key is wrong or revoked",
                403: "the API key lacks a permission (album.read, asset.read)",
                404: "wrong IMMICH_URL?"}.get(code, "")
        super().__init__(f"Immich answered {code} on {path.split('?')[0]}" + (f": {hint}" if hint else ""))
        self.code = code


def _slim(a: dict) -> dict:
    return {"id": a.get("id"), "path": a.get("originalPath") or "",
            "offline": bool(a.get("isOffline")), "trashed": bool(a.get("isTrashed"))}


def fetch_assets(api: Api, prefix: Optional[str]) -> tuple[list, str]:
    """Assets of the Facebook folders. Immich v3 search (cursor + filter),
    limited to the downloads' path when it is known; older servers: classic
    paged search over the whole library, filtered here."""
    out = []
    flt = {"visibility": {"in": ["timeline", "archive"]}}
    if prefix:
        flt["originalPath"] = {"startsWith": prefix}
    try:
        cursor = None
        while True:
            body = {"filter": flt, "size": PAGE}
            if cursor:
                body["cursor"] = cursor
            r = (api.call("POST", "/search/metadata", body) or {}).get("assets") or {}
            out += [_slim(a) for a in r.get("items") or []]
            cursor = r.get("nextCursor")
            if not cursor:
                return out, "filter"
    except ImmichHTTP as e:
        if e.code != 400:
            raise
    out = []
    for vis in ("timeline", "archive"):
        page = 1
        while page:
            r = (api.call("POST", "/search/metadata", {"page": page, "size": PAGE, "visibility": vis})
                 or {}).get("assets") or {}
            out += [_slim(a) for a in r.get("items") or []
                    if not prefix or (a.get("originalPath") or "").startswith(prefix)]
            nxt = r.get("nextPage")
            page = int(nxt) if nxt else 0
    return out, "paged"


def guess_prefix(path: str) -> Optional[str]:
    """'/x/facebook/Account/Album (a.1)/f.jpg' -> '/x/facebook/'"""
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 4 and SID_DIR.search(parts[-2]):
        return "/".join(parts[:-3]) + "/"
    return None


@dataclass
class Snapshot:
    taken: float
    albums: list
    assets: list
    prefix: Optional[str]
    method: str
    duration: float


class ImmichWatcher:
    def __init__(self, url: Optional[str], key_file: str, every: float = 1800):
        self.url = (url or "").strip()
        self.key_file = key_file
        self.every = every
        self.lock = threading.Lock()
        self.snap: Optional[Snapshot] = None
        self.error: Optional[str] = None
        self.last_try: Optional[float] = None
        self.busy = False
        self._prefix: Optional[str] = None
        self._wake = threading.Event()

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def refresh(self):
        with self.lock:
            if self.busy:
                return
            self.busy = True
        t0 = time.monotonic()
        try:
            api = Api(self.url, read_key(self.key_file))
            albums = api.call("GET", "/albums") or []
            prefix = self._prefix
            if not prefix:
                prefix = self._find_prefix(api, albums)
            assets, method = fetch_assets(api, prefix)
            if prefix and not assets and self._prefix:
                # the library moved: look again over everything
                self._prefix = None
                assets, method = fetch_assets(api, None)
                prefix = None
            if not prefix:
                for a in assets:
                    prefix = guess_prefix(a["path"])
                    if prefix:
                        break
            assets = [a for a in assets if SID_DIR.search(os.path.basename(os.path.dirname(a["path"])))]
            snap = Snapshot(time.time(), [{k: a.get(k) for k in ("id", "albumName", "assetCount", "description",
                                                                 "order", "albumThumbnailAssetId")}
                                          for a in albums],
                            assets, prefix, method, time.monotonic() - t0)
            with self.lock:
                self.snap, self.error, self._prefix = snap, None, prefix
        except ImmichError as e:
            with self.lock:
                self.error = str(e)
        except Exception as e:   # never let the thread die
            with self.lock:
                self.error = f"unexpected error: {e.__class__.__name__}"
        finally:
            with self.lock:
                self.busy = False
                self.last_try = time.time()

    def _find_prefix(self, api: Api, albums: list) -> Optional[str]:
        """Where the downloads live in Immich, from the cover of an album made
        from a Facebook folder (its description link, else "(a.N)" in its
        name). None: search the whole library once."""
        def score(a):
            return 0 if SET_IN_DESC.search(a.get("description") or "") else 1
        for a in sorted(albums, key=score)[:5]:
            thumb = a.get("albumThumbnailAssetId")
            if not thumb:
                continue
            try:
                p = (api.call("GET", f"/assets/{thumb}") or {}).get("originalPath") or ""
            except ImmichHTTP:
                continue
            pre = guess_prefix(p)
            if pre:
                return pre
        return None

    def wake(self):
        self._wake.set()

    def run(self):
        while True:
            if self.configured:
                self.refresh()
            self._wake.wait(self.every)
            self._wake.clear()


# ---------------------------------------------------------------- report
@dataclass
class AlbumCheck:
    set_id: str
    expected: str = ""
    folder: str = ""
    disk: int = 0
    immich: int = 0
    offline: int = 0
    missing_ids: list = field(default_factory=list)     # on disk, not in Immich
    gone_ids: list = field(default_factory=list)        # in Immich, not on disk any more
    folders_in_immich: int = 0
    albums: list = field(default_factory=list)          # [{"name","count","order","id"}]
    issues: list = field(default_factory=list)          # [(severity, text)]

    @property
    def severity(self) -> str:
        sevs = {s for s, _ in self.issues}
        return "bad" if "bad" in sevs else "warn" if "warn" in sevs else "info" if "info" in sevs else "ok"


@dataclass
class Report:
    rows: list
    unlinked: list          # Immich albums not linked to any folder: [{"name","count"}]
    totals: dict

    def counts(self) -> dict:
        c = {}
        for r in self.rows:
            c[r.severity] = c.get(r.severity, 0) + 1
        return c


def build_report(snap: Snapshot, disk_albums: dict, chosen: dict, want_order: str = "asc") -> Report:
    """disk_albums: {set_id: AlbumDir}; chosen: {set_id: (name, source)}."""
    by_sid: dict[str, list] = {}
    folders: dict[str, set] = {}
    for a in snap.assets:
        d = os.path.dirname(a["path"].replace("\\", "/"))
        m = SID_DIR.search(os.path.basename(d))
        if not m:
            continue
        by_sid.setdefault(m.group(1), []).append(a)
        folders.setdefault(m.group(1), set()).add(d)

    sids = [s for s in disk_albums if not s.startswith("?")]
    names = {}
    for sid in sids:
        d = disk_albums[sid]
        for n in (chosen.get(sid, ("",))[0], d.folder, SID_DIR.sub("", d.folder).strip()):
            if n:
                names.setdefault(_norm(n), sid)
    linked: dict[str, list] = {}
    unlinked = []
    for al in snap.albums:
        name = al.get("albumName") or ""
        m = SET_IN_DESC.search(al.get("description") or "")
        sid = m.group(1) if m and m.group(1) in disk_albums else None
        sid = sid or names.get(_norm(name))
        if not sid:
            sid = next((s for s in sids if f"({s})" in name), None)
        rec = {"id": al.get("id"), "name": name, "count": al.get("assetCount"), "order": al.get("order")}
        if sid:
            linked.setdefault(sid, []).append(rec)
        else:
            unlinked.append(rec)

    rows = []
    for sid in sids:
        d = disk_albums[sid]
        exp = chosen.get(sid, (SID_DIR.sub("", d.folder).strip(), ""))[0]
        ims = [a for a in by_sid.get(sid, []) if not a["trashed"]]
        imm_ids = {m.group(1) for a in ims if (m := RE_ID.search(os.path.basename(a["path"])))}
        r = AlbumCheck(sid, exp, d.folder, d.media, len(ims), sum(a["offline"] for a in ims),
                       sorted(set(d.media_ids) - imm_ids), sorted(imm_ids - set(d.media_ids)),
                       len(folders.get(sid, ())), sorted(linked.get(sid, []), key=lambda x: x["name"]))
        if d.media and not ims:
            r.issues.append(("bad", "not in Immich: library not scanned yet, or this folder is outside the external library"))
        elif r.missing_ids:
            r.issues.append(("warn", f"{len(r.missing_ids)} photo(s) on disk not in Immich yet (library scan pending?)"))
        if r.offline:
            r.issues.append(("bad", f"{r.offline} photo(s) offline in Immich (file moved or unreadable)"))
        if r.gone_ids:
            r.issues.append(("warn", f"{len(r.gone_ids)} photo(s) in Immich no longer on disk"))
        if r.folders_in_immich > 1:
            r.issues.append(("warn", f"photos from {r.folders_in_immich} different paths in Immich (folder renamed or mounted twice?)"))
        if ims and not r.albums:
            r.issues.append(("warn", "no album in Immich: the album creator has not run since, or does not see the .albumprops"))
        if len(r.albums) > 1:
            r.issues.append(("warn", f"{len(r.albums)} albums for this folder: delete the one(s) not named “{exp}”"))
        good = [a for a in r.albums if _norm(a["name"]) == _norm(exp)]
        if r.albums and not good:
            r.issues.append(("warn", f"album named “{r.albums[0]['name']}” instead of “{exp}”: .albumprops not "
                                     f"applied (READ_ALBUM_PROPERTIES, volume) or written after the album was created"))
        for a in good[:1]:
            if a["count"] is not None and ims and a["count"] < len(ims):
                r.issues.append(("warn", f"the album has {a['count']} of the {len(ims)} photos in Immich "
                                         f"(the album creator has not run since the last scan?)"))
            if want_order and a.get("order") and a["order"] != want_order:
                r.issues.append(("info", "album shows newest first (sort_order not applied: "
                                         "UPDATE_ALBUM_PROPS_MODE=1 for one run)"))
        rows.append(r)
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r.severity], r.expected.casefold()))
    totals = {"disk": sum(r.disk for r in rows), "immich": sum(r.immich for r in rows),
              "albums": sum(len(r.albums) for r in rows), "folders": len(rows),
              "assets_seen": len(snap.assets), "immich_albums": len(snap.albums)}
    return Report(rows, sorted(unlinked, key=lambda x: _norm(x["name"])), totals)
