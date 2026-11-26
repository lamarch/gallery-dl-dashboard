"""fb-dl dashboard: read-only web UI over gallery-dl's log, albums.txt and disk."""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from collections import OrderedDict

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import albums as albums_mod
from . import disk as disk_mod
from .integrity import IntegrityScanner
from .acks import Acks
from .fbdata import FbStore
from .fbpages import parse_albums_page
from .ignores import Ignores
from .immich import ImmichWatcher, build_report as build_immich_report
from .names import NamesFile, album_names, albumprops_content, read_albumprops
from .logparse import END_REASONS, LogTail, decode_exit, set_log_tz
from .status import AlbumRow, album_rows, event_counts, header, log_snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = {
    "log_file": os.environ.get("LOG_FILE", "/downloads/logs/fb-dl.log"),
    "albums_file": os.environ.get("ALBUMS_FILE", "/input/albums.txt"),
    "downloads_root": os.environ.get("DOWNLOADS_ROOT", "/downloads/facebook"),
    "fs_path": os.environ.get("DISK_PATH", "/downloads"),
    "base": os.environ.get("BASE_PATH", "").rstrip("/"),
    "poll": float(os.environ.get("POLL_SECONDS", "2")),
    "disk_every": float(os.environ.get("DISK_SCAN_SECONDS", "60")),
    "data_dir": os.environ.get("DATA_DIR", "/data"),
    "archive_file": os.environ.get("ARCHIVE_FILE", "/downloads/logs/fb-archive.sqlite3"),
    "integrity": os.environ.get("INTEGRITY", "1") != "0",
    "integrity_mbps": float(os.environ.get("INTEGRITY_MAX_MBPS", "40")),
    "integrity_every": float(os.environ.get("INTEGRITY_RESCAN_SECONDS", "900")),
    # the settings fix_order.py is run with (--tz / --step)
    "fix_tz": os.environ.get("FIXORDER_TZ") or os.environ.get("DISPLAY_TZ") or os.environ.get("TZ") or "UTC",
    "fix_step": float(os.environ.get("FIXORDER_STEP", "1")),
    # host-side folders, only used to print ready-to-paste commands
    "host_downloads": os.environ.get("HOST_DOWNLOADS_DIR", "./downloads"),
    "host_data": os.environ.get("HOST_DATA_DIR", "./dashboard-data"),
    "host_tools": os.environ.get("HOST_TOOLS_DIR", "./web-dashboard/tools"),
    "host_fixorder": os.environ.get("HOST_FIXORDER", "./web-dashboard/tools/fix_order.py"),
    "immich_url": os.environ.get("IMMICH_URL", ""),
    "immich_key_file": os.environ.get("IMMICH_API_KEY_FILE", "/run/secrets/immich_api_key"),
    "immich_every": float(os.environ.get("IMMICH_REFRESH_SECONDS", "1800")),
}


def _tz(name, fallback):
    try:
        return ZoneInfo(name) if name else fallback
    except Exception:
        return fallback


DISPLAY_TZ = _tz(os.environ.get("DISPLAY_TZ") or os.environ.get("TZ"), timezone.utc)
set_log_tz(_tz(os.environ.get("LOG_TZ", "UTC"), timezone.utc))


class Monitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.tail = LogTail(CFG["log_file"])
        self.albums, self.albums_error, self._albums_mtime = [], None, None
        self.disk = None
        self._last_disk = 0.0
        self.started = time.time()
        self.stop = threading.Event()
        self.last_error = None

    def refresh(self):
        with self.lock:
            self.tail.poll()
            m = albums_mod.input_mtime(CFG["albums_file"])
            if m != self._albums_mtime:
                self.albums, self.albums_error = albums_mod.read_albums(CFG["albums_file"])
                self._albums_mtime = m
        if time.time() - self._last_disk >= CFG["disk_every"]:
            snap = disk_mod.scan(CFG["downloads_root"], CFG["fs_path"])   # outside lock
            with self.lock:
                self.disk = snap
            self._last_disk = time.time()

    def run(self):
        while not self.stop.is_set():
            try:
                self.refresh()
                self.last_error = None
            except Exception as e:  # keep serving whatever we have
                self.last_error = f"{e.__class__.__name__}: {e}"
            self.stop.wait(CFG["poll"])


MON = Monitor()


def current_set():
    s = MON.tail.state.session
    j = s.current_job if s else None
    return j.set_id if j else None


def _locked_current_set():
    with MON.lock:
        return current_set()


_cache_path = (os.path.join(CFG["data_dir"], "integrity.sqlite3")
               if os.path.isdir(CFG["data_dir"]) and os.access(CFG["data_dir"], os.W_OK) else None)
def _locked_log_snapshot():
    with MON.lock:
        return log_snapshot(MON.tail.state)


NAMES = NamesFile(os.path.join(CFG["data_dir"], "album-names.tsv")
                  if os.path.isdir(CFG["data_dir"]) and os.access(CFG["data_dir"], os.W_OK) else None)


def same_origin(request: Request) -> bool:
    """Reject cross-site form posts (the browser re-sends basic-auth credentials)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    from urllib.parse import urlsplit
    return bool(origin) and urlsplit(origin).netloc == host


ACKS = Acks(os.path.join(CFG["data_dir"], "acknowledged-events.json")
            if os.path.isdir(CFG["data_dir"]) and os.access(CFG["data_dir"], os.W_OK) else None)

FBSTORE = FbStore(os.path.join(CFG["data_dir"], "facebook-albums.json")
                  if os.path.isdir(CFG["data_dir"]) and os.access(CFG["data_dir"], os.W_OK) else None)

IGNORES = Ignores(os.path.join(CFG["data_dir"], "ignored-missing.tsv")
                  if os.path.isdir(CFG["data_dir"]) and os.access(CFG["data_dir"], os.W_OK) else None)

SCANNER = IntegrityScanner(CFG["downloads_root"], _cache_path, CFG["archive_file"],
                           CFG["integrity_mbps"], CFG["integrity_every"], _locked_current_set,
                           log_snapshot=_locked_log_snapshot,
                           fix_tz=_tz(CFG["fix_tz"], timezone.utc), fix_tz_name=CFG["fix_tz"],
                           fix_step=CFG["fix_step"], ignored=IGNORES.snapshot,
                           fb_counts=FBSTORE.counts)


IMMICH = ImmichWatcher(CFG["immich_url"], CFG["immich_key_file"], CFG["immich_every"])
_IMMICH_REPORT = {"key": None, "report": None}


def immich_report():
    """Immich snapshot compared with the disk and the chosen names (cached
    until one of them changes)."""
    snap, disk = IMMICH.snap, MON.disk
    if not snap or not disk:
        return None
    key = (snap.taken, disk.taken, NAMES.mtime)
    if _IMMICH_REPORT["key"] != key:
        try:
            _rows, _props, chosen = _names_rows()
        except OSError:
            chosen = {}
        _IMMICH_REPORT.update(key=(snap.taken, disk.taken, NAMES.mtime),
                              report=build_immich_report(snap, disk.albums, chosen))
    return _IMMICH_REPORT["report"]


@asynccontextmanager
async def lifespan(app):
    MON.refresh()
    t = threading.Thread(target=MON.run, daemon=True, name="monitor")
    t.start()
    if CFG["integrity"]:
        threading.Thread(target=SCANNER.run, daemon=True, name="integrity").start()
    if IMMICH.configured:
        threading.Thread(target=IMMICH.run, daemon=True, name="immich").start()
    yield
    MON.stop.set()
    SCANNER.stop.set()
    SCANNER.poke.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# ---- template helpers -------------------------------------------------------
def fmt_dt(d, fmt="%Y-%m-%d %H:%M:%S"):
    return d.astimezone(DISPLAY_TZ).strftime(fmt) if d else "–"


def fmt_bytes(n):
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000


def fmt_dur(sec):
    if sec is None:
        return "–"
    sec = int(max(sec, 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_int(n):
    return "–" if n is None else f"{n:,}".replace(",", " ")


def ago(d, now=None):
    if not d:
        return "–"
    now = now or datetime.now(timezone.utc)
    return fmt_dur((now - d).total_seconds()) + " ago"


def host_path(folder_rel: str) -> str:
    """Host-side path of an album folder (relative to DOWNLOADS_ROOT)."""
    sub = os.path.relpath(CFG["downloads_root"], CFG["fs_path"])
    return "/".join(p.strip("/") if i else p.rstrip("/")
                    for i, p in enumerate([CFG["host_downloads"], sub, folder_rel]) if p not in ("", "."))


def fixorder_cmd(folders: list, dry: bool) -> str:
    """Ready-to-paste fix_order.py command line, every path shell-quoted."""
    import shlex
    step = "%g" % CFG["fix_step"]
    args = ["python3", CFG["host_fixorder"], "--tz", CFG["fix_tz"], "--step", step]
    if dry:
        args.append("--dry-run")
    args += [host_path(f) for f in folders]
    return " ".join(shlex.quote(a) for a in args)


templates.env.filters.update(dt=fmt_dt, bytes=fmt_bytes, dur=fmt_dur, n=fmt_int, ago=ago)
def albumprops_cmd(write: bool) -> str:
    """Ready-to-paste apply_albumprops.py command, run with the host's python3
    from the compose folder (dry run unless writing)."""
    import shlex
    args = ["python3", f"{CFG['host_tools'].rstrip('/')}/apply_albumprops.py",
            "--root", host_path(""), "--names", f"{CFG['host_data'].rstrip('/')}/album-names.tsv"]
    if write:
        args.append("--write")
    return " ".join(shlex.quote(a) for a in args)


def _static_versions() -> dict:
    """Content hash of each static file: changes the URL whenever the file
    changes, so browsers never keep an old script or stylesheet."""
    import hashlib
    out = {}
    d = os.path.join(HERE, "static")
    for n in os.listdir(d):
        with open(os.path.join(d, n), "rb") as fh:
            out[n] = hashlib.sha1(fh.read()).hexdigest()[:10]
    return out


STATIC_V = _static_versions()


def static_url(name: str) -> str:
    return f"{CFG['base']}/static/{name}?v={STATIC_V.get(name, '0')}"


def album_title(set_id, fallback=None):
    """Facebook's title when an albums page was imported (gallery-dl's title
    can be badly decoded), else the fallback."""
    return FBSTORE.titles().get(set_id) or fallback or set_id


templates.env.globals.update(fixorder_cmd=fixorder_cmd, albumprops_cmd=albumprops_cmd,
                             static_url=static_url, album_title=album_title)
templates.env.globals.update(base=CFG["base"], END_REASONS=END_REASONS, decode_exit=decode_exit,
                             tzname=str(DISPLAY_TZ))


def ctx(request, **kw):
    now = datetime.now(timezone.utc)
    acked = kw.pop("acked", None) or ACKS.checker()
    with MON.lock:
        st = MON.tail.state
        h = header(st, MON.albums, MON.disk, now, acked=acked, fb=FBSTORE.counts())
        if h.set_id:
            h.title = FBSTORE.titles().get(h.set_id) or h.title
        kw.update(ir=SCANNER.report, ip=SCANNER.progress, scanner=SCANNER,
                  integrity_on=CFG["integrity"])
    try:
        ireport = immich_report() if IMMICH.configured else None
    except Exception:            # a page must never fail because of Immich
        ireport = None
    kw.update(immich_on=IMMICH.configured, immich_bad=(ireport.counts().get("bad", 0) + ireport.counts().get("warn", 0))
              if ireport else 0)
    with MON.lock:
        kw.update(request=request, h=h, now=now, st=st, mon=MON, tail=MON.tail,
                  albums_error=MON.albums_error, disk=MON.disk,
                  acked=acked, acks_writable=bool(ACKS.path),
                  bad_events=sum(1 for e in st.events if e.severity == "bad" and not acked(e)))
        return kw


# ---- pages --------------------------------------------------------------------
def attach_checks(rows, flags=True):
    """Add the disk-check verdict of each album to its row (and to its flags)."""
    rep = SCANNER.report
    for r in rows:
        ac = rep.albums.get(r.set_id) if rep and r.set_id else None
        r.check = ac
        if not flags or ac is None or ac.verdict not in ("problems", "warnings"):
            continue
        sev = "bad" if ac.verdict == "problems" else "warn"
        # the Facebook count is shown in its own column ("Expected")
        texts = [i.text for i in ac.issues if i.severity == sev and i.kind != "fb_count"]
        if not texts:
            continue
        r.flags.append((sev, "disk check: " + "; ".join(texts[:3]) + ("…" if len(texts) > 3 else "")))
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with MON.lock:
        st = MON.tail.state
        rows = album_rows(st, MON.albums, MON.disk, current_set(),
                          fb=FBSTORE.counts(), fb_titles=FBSTORE.titles())
        acked = ACKS.checker()
        recent = [e for e in reversed(st.events) if e.severity != "info" and not acked(e)][:15]
    attach_checks(rows)
    flagged = [r for r in rows if r.severity in ("bad", "warn")]
    return templates.TemplateResponse(request, "index.html", ctx(
        request, rows=rows, recent=recent, flagged=flagged, acked=acked, page="albums"))


@app.get("/album/{set_id}", response_class=HTMLResponse)
def album(request: Request, set_id: str):
    with MON.lock:
        st = MON.tail.state
        rows = album_rows(st, MON.albums, MON.disk, current_set(),
                          fb=FBSTORE.counts(), fb_titles=FBSTORE.titles())
        row = next((r for r in rows if r.set_id == set_id), None)
        jobs = [j for j in st.jobs if j.set_id == set_id]
        events = [e for e in st.events if e.set_id == set_id]
        d = MON.disk.albums.get(set_id) if MON.disk else None
    rep = SCANNER.report
    ac = rep.albums.get(set_id) if rep else None
    if row is None and ac is not None:        # on disk only (not in albums.txt nor the log)
        row = AlbumRow(None, set_id, ac.username, album_title(set_id, ac.title),
                       ac.folders[0] if ac.folders else None, "unlisted")
    if row is None:
        raise HTTPException(404, "unknown album")
    attach_checks([row], flags=False)   # details are shown in the disk-check section
    return templates.TemplateResponse(request, "album.html", ctx(
        request, row=row, jobs=jobs, events=list(reversed(events))[:300], d=d, ac=ac,
        ad=(rep.dates.albums.get(set_id) if rep and rep.dates else None), cfg=CFG,
        rescanning=set_id in RESCANS,
        page="albums"))


@app.get("/integrity", response_class=HTMLResponse)
def integrity_page(request: Request):
    rep = SCANNER.report
    albums = sorted(rep.albums.values(), key=lambda a: (
        {"problems": 0, "warnings": 1, "scanning": 2, "in progress": 3, "verified": 4}[a.verdict],
        a.username or "", a.title or "", a.set_id)) if rep else []
    return templates.TemplateResponse(request, "integrity.html", ctx(
        request, albums=albums, page="integrity"))


@app.get("/dates", response_class=HTMLResponse)
def dates_page(request: Request, sel: list[str] = Query(default=[])):
    rep = SCANNER.report
    dr = rep.dates if rep else None
    order = {"changed": 0, "partial": 1, "not run": 2, "applied": 3, "nothing": 4}
    albums = sorted(dr.albums.values(), key=lambda a: (order.get(a.state, 9), a.set_id)) if dr else []
    runnable, folders = safe_to_fix(rep, albums)
    # albums chosen by hand ("force"): build the command and spell out every risk
    chosen, chosen_folders, risks = [], [], []
    for a in albums:
        if a.set_id not in sel:
            continue
        ac = rep.albums.get(a.set_id) if rep else None
        if not ac or not ac.folders:
            risks.append(("bad", a.set_id, "no folder on disk: left out"))
            continue
        chosen.append(a)
        chosen_folders += ac.folders
        if not a.ready:
            risks.append(("warn", a.set_id, f"not ready: {a.ready_reason}. If the album changes later, "
                                            f"a new run would rewrite photos corrected now."))
        if a.danger:
            what = ("mtime only" if not a.danger_content else
                    "all with a file content rewrite" if a.danger_content == a.danger else
                    f"{a.danger_content} with a file content rewrite, the others mtime only")
            risks.append(("bad", a.set_id, f"{a.danger} already-corrected photo(s) would be modified "
                                           f"({what}): Immich will re-process them"))
        if a.rounded:
            risks.append(("info", a.set_id, f"{a.rounded} rounded mtime(s) would be restored: "
                                            f"Immich will re-process those photos (timeline unchanged)"))
        if a.state == "applied" and not a.rounded:
            risks.append(("info", a.set_id, "already applied: the run changes nothing"))
    return templates.TemplateResponse(request, "dates.html", ctx(
        request, dr=dr, dalbums=albums, cfg=CFG, runnable=runnable, run_folders=folders,
        sel=set(sel), chosen=chosen, chosen_folders=chosen_folders, risks=risks, page="dates"))


def safe_to_fix(rep, albums):
    """Albums fix_order.py can be run on now without touching a corrected file:
    proven complete ('ready'), not run or partial, nothing already corrected
    would change."""
    runnable, folders = [], []
    for a in albums:
        ac = rep.albums.get(a.set_id) if rep else None
        if a.ready and a.state in ("not run", "partial") and a.danger == 0 and ac and ac.folders:
            runnable.append(a)
            folders += ac.folders
    return runnable, folders


def _names_rows():
    """Album names merged with what is on disk now."""
    disk = MON.disk
    albums = {}
    if disk:
        for sid, a in disk.albums.items():
            if not sid.startswith("?"):
                albums[sid] = (a.folder, a.title)
    fbt = FBSTORE.titles()
    rows = NAMES.sync(albums, write=True, fb_titles=fbt) if NAMES.path else NAMES.sync(albums, fb_titles=fbt)
    props, paths = {}, {}
    for r in rows:
        a = disk.albums.get(r.set_id) if disk else None
        props[r.set_id] = read_albumprops(a.path) if a else ("none", None)
        if a:
            paths[r.set_id] = a.path
    chosen = album_names(rows, paths)
    for sid, (name, _src) in chosen.items():
        st, val = props.get(sid, ("none", None))
        if st == "ours" and val == name:
            try:
                with open(os.path.join(paths[sid], ".albumprops"), encoding="utf-8") as fh:
                    if fh.read() != albumprops_content(name, sid):
                        props[sid] = ("old", val)        # right name, older set of properties
            except OSError:
                props[sid] = ("unreadable", None)
    return rows, props, chosen


@app.get("/names", response_class=HTMLResponse)
def names_page(request: Request, saved: str = "", error: str = ""):
    try:
        rows, props, chosen = _names_rows()
    except OSError as e:
        rows, props, chosen, error = [], {}, {}, f"cannot write album-names.tsv: {e}"
    return templates.TemplateResponse(request, "names.html", ctx(
        request, rows=rows, props=props, chosen=chosen, names=NAMES, dups=NAMES.duplicates(),
        saved=saved, error=error or NAMES.error, cfg=CFG, page="names"))


@app.post("/names/{set_id}")
def names_save(request: Request, set_id: str, custom_name: str = Form("")):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    from urllib.parse import quote
    try:
        NAMES.set_custom(set_id, custom_name)
        return RedirectResponse(f"{CFG['base']}/names?saved={quote(set_id)}#a-{quote(set_id)}", status_code=303)
    except KeyError:
        raise HTTPException(404, "unknown album")
    except (ValueError, OSError) as e:
        return RedirectResponse(f"{CFG['base']}/names?error={quote(str(e))}", status_code=303)


RESCANS: set = set()      # albums with a re-check queued or running


@app.post("/album/{set_id}/rescan")
def album_rescan(request: Request, set_id: str):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    if set_id not in RESCANS:
        RESCANS.add(set_id)

        def job():
            try:
                if not SCANNER.rescan_album(set_id):
                    SCANNER.poke.set()          # no full pass yet: start one
            except Exception as e:
                SCANNER.last_error = f"album re-check: {e.__class__.__name__}: {e}"
            finally:
                RESCANS.discard(set_id)
        threading.Thread(target=job, daemon=True, name=f"rescan-{set_id}").start()
    from urllib.parse import quote
    return RedirectResponse(f"{CFG['base']}/album/{quote(set_id)}#disk-check", status_code=303)


def _rebuild_soon():
    """Recompute the report in the background (no file is re-read)."""
    def job():
        if SCANNER.scan_lock.acquire(blocking=False):   # a running pass will include it anyway
            try:
                SCANNER.rebuild()
            except Exception as e:
                SCANNER.last_error = f"rebuild: {e.__class__.__name__}: {e}"
            finally:
                SCANNER.scan_lock.release()
    threading.Thread(target=job, daemon=True).start()


@app.post("/album/{set_id}/ignore")
def album_ignore(request: Request, set_id: str, photo_id: str = Form(...), reason: str = Form(""),
                 undo: str = Form("")):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    if not photo_id.isdigit():
        raise HTTPException(400, "invalid photo id")
    try:
        if undo:
            IGNORES.remove(set_id, photo_id)
        else:
            IGNORES.add(set_id, photo_id, reason or "not part of this album")
    except OSError as e:
        raise HTTPException(500, str(e))
    _rebuild_soon()
    from urllib.parse import quote
    return RedirectResponse(f"{CFG['base']}/album/{quote(set_id)}#disk-check", status_code=303)


MAX_PAGE_BYTES = 80 * 1024 * 1024


@app.get("/facebook", response_class=HTMLResponse)
def facebook_page(request: Request, done: str = "", error: str = ""):
    fb = FBSTORE.snapshot()
    rep = SCANNER.report
    with MON.lock:
        listed = {a.set_id: a for a in MON.albums}
    pages = {}
    for sid, v in fb.items():
        ac = rep.albums.get(sid) if rep else None
        li = listed.get(sid)
        status = ("downloaded" if ac is not None and (li is None or li.done or ac.media) else
                  "queued" if li is not None else "not downloaded")
        pages.setdefault(v.get("page") or "?", []).append(
            {"sid": sid, "fb": v, "ac": ac, "listed": li, "status": status})
    summaries = {}
    for name, rows in pages.items():
        rows.sort(key=lambda r: (r["fb"].get("pos") or 10**6, r["sid"]))
        no_order = any(not r["fb"].get("pos") for r in rows)   # imported before positions were kept
        taken = [r["fb"].get("pos") for r in rows if r["status"] != "not downloaded" and r["fb"].get("pos")]
        if taken:
            lo, hi = min(taken), max(taken)
            gaps = [r for r in rows if r["status"] == "not downloaded" and lo < (r["fb"].get("pos") or 0) < hi]
            summaries[name] = {"first": lo, "last": hi, "n": len(taken), "gaps": gaps, "total": len(rows)}
        else:
            summaries[name] = {"first": None, "last": None, "n": 0, "gaps": [], "total": len(rows)}
        summaries[name]["no_order"] = no_order
    return templates.TemplateResponse(request, "facebook.html", ctx(
        request, pages=dict(sorted(pages.items())), summaries=summaries, done=done, error=error,
        fb_writable=bool(FBSTORE.path), page="facebook"))


@app.post("/facebook/import")
async def facebook_import(request: Request, files: list[UploadFile] = File(...)):
    """Parse saved pages IN MEMORY; only album IDs, titles and counts are kept."""
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    from urllib.parse import quote
    msgs = []
    try:
        for f in files:
            raw = await f.read(MAX_PAGE_BYTES + 1)
            await f.close()
            if len(raw) > MAX_PAGE_BYTES:
                msgs.append(f"{f.filename}: too large")
                continue
            page = parse_albums_page(raw.decode("utf-8", errors="replace"))
            del raw
            if not page.albums:
                msgs.append(f"{f.filename}: " + "; ".join(page.warnings))
                continue
            r = FBSTORE.import_page(page)
            msgs.append(f"{f.filename}: {r['albums']} album(s), {r['new']} new, {r['updated']} updated"
                        + (" (" + "; ".join(page.warnings) + ")" if page.warnings else ""))
    except OSError as e:
        return RedirectResponse(f"{CFG['base']}/facebook?error={quote(str(e))}", status_code=303)
    _rebuild_soon()
    return RedirectResponse(f"{CFG['base']}/facebook?done={quote(' | '.join(msgs))}", status_code=303)


@app.post("/facebook/forget")
def facebook_forget(request: Request, page: str = Form(...)):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    FBSTORE.forget_page(page)
    _rebuild_soon()
    return RedirectResponse(f"{CFG['base']}/facebook", status_code=303)


@app.post("/integrity/rescan")
def integrity_rescan(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    SCANNER.poke.set()
    return RedirectResponse(f"{CFG['base']}/integrity", status_code=303)


_THUMBS: "OrderedDict[str, bytes]" = OrderedDict()
_THUMBS_LOCK = threading.Lock()


@app.get("/thumb/{key}")
def thumb(key: str):
    """Small JPEG preview. Only files known to the last scan can be served."""
    rep = SCANNER.report
    path = rep.by_key.get(key) if rep else None
    if not path:
        raise HTTPException(404)
    with _THUMBS_LOCK:
        data = _THUMBS.get(key)
    if data is None:
        try:
            from PIL import Image
            import io
            with Image.open(path) as im:
                im.draft("RGB", (480, 480))
                im.thumbnail((240, 240))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=80)
                data = buf.getvalue()
        except Exception:
            raise HTTPException(404, "no preview")
        with _THUMBS_LOCK:
            _THUMBS[key] = data
            while len(_THUMBS) > 400:
                _THUMBS.popitem(last=False)
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/sessions", response_class=HTMLResponse)
def sessions(request: Request):
    with MON.lock:
        ss = list(reversed(MON.tail.state.sessions))
    return templates.TemplateResponse(request, "sessions.html", ctx(request, sessions=ss, page="sessions"))


def _filtered_events(st, kind, sev, show, acked):
    return [e for e in reversed(st.events)
            if (not kind or e.kind == kind) and (not sev or e.severity == sev)
            and (show == "all" or (show == "acked") == acked(e))]


@app.get("/events", response_class=HTMLResponse)
def events(request: Request, kind: str = "", sev: str = "", show: str = "new", limit: int = 500):
    acked = ACKS.checker()
    with MON.lock:
        st = MON.tail.state
        allf = _filtered_events(st, kind, sev, show, acked)
        evs = allf[:min(limit, 5000)]
        counts = event_counts(st)
        n_new = sum(1 for e in st.events if not acked(e) and e.severity != "info")
        n_acked = sum(1 for e in st.events if acked(e))
        n_ackable = sum(1 for e in allf if sev == "info" or e.severity != "info") if show == "new" else 0
    return templates.TemplateResponse(request, "events.html", ctx(
        request, evs=evs, total=len(allf), counts=counts, kind=kind, sev=sev, show=show,
        n_new=n_new, n_acked=n_acked, n_ackable=n_ackable, acked=acked, page="events"))


def _back(request: Request, default: str) -> str:
    """Return to the page the form was posted from (same site only)."""
    from urllib.parse import urlsplit
    ref = request.headers.get("referer") or ""
    u = urlsplit(ref)
    if u.path.startswith(CFG["base"] + "/"):
        return u.path + (("?" + u.query) if u.query else "")
    return default


@app.post("/events/ack")
def events_ack(request: Request, key: list[str] = Form(default=[]), undo: str = Form(""),
               scope: str = Form(""), kind: str = Form(""), sev: str = Form("")):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    try:
        if scope == "filtered":
            # every currently NEW event matching the filter (info events included
            # only when the filter explicitly asks for them)
            acked = ACKS.checker()
            with MON.lock:
                keys = [e.key for e in _filtered_events(MON.tail.state, kind, sev, "new", acked)
                        if sev == "info" or e.severity != "info"]
            ACKS.ack(keys)
        elif undo:
            ACKS.unack(key)
        else:
            ACKS.ack(key)
    except OSError as e:
        raise HTTPException(500, str(e))
    return RedirectResponse(_back(request, f"{CFG['base']}/events"), status_code=303)


@app.get("/immich", response_class=HTMLResponse)
def immich_page(request: Request, show: str = "issues"):
    rep = immich_report() if IMMICH.configured else None
    rows = rep.rows if rep else []
    if show != "all":
        rows = [r for r in rows if r.severity != "ok"]
    return templates.TemplateResponse(request, "immich.html", ctx(
        request, w=IMMICH, rep=rep, rows=rows, show=show, cfg=CFG, page="immich"))


@app.post("/immich/refresh")
def immich_refresh(request: Request):
    if not same_origin(request):
        raise HTTPException(403, "cross-site request refused")
    IMMICH.wake()
    return RedirectResponse(f"{CFG['base']}/immich", status_code=303)


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request):
    with MON.lock:
        st = MON.tail.state
        unrec = sorted(st.unrecognized.items(), key=lambda kv: -kv[1])
    return templates.TemplateResponse(request, "diagnostics.html", ctx(
        request, unrec=unrec, cfg=CFG, page="diagnostics"))


@app.get("/fragment/header", response_class=HTMLResponse)
def fragment_header(request: Request):
    return templates.TemplateResponse(request, "_header.html", ctx(request))


@app.get("/api/status")
def api_status():
    with MON.lock:
        h = header(MON.tail.state, MON.albums, MON.disk)
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in h.__dict__.items()}
    return JSONResponse(out)


@app.get("/healthz")
def healthz():
    return {"ok": MON.last_error is None, "error": MON.last_error,
            "integrity_error": SCANNER.last_error}
