"""fb-dl dashboard: read-only web UI over gallery-dl's log, albums.txt and disk."""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import albums as albums_mod
from . import disk as disk_mod
from .integrity import IntegrityScanner
from .logparse import END_REASONS, LogTail, decode_exit, set_log_tz
from .status import AlbumRow, album_rows, event_counts, header

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
SCANNER = IntegrityScanner(CFG["downloads_root"], _cache_path, CFG["archive_file"],
                           CFG["integrity_mbps"], CFG["integrity_every"], _locked_current_set)


@asynccontextmanager
async def lifespan(app):
    MON.refresh()
    t = threading.Thread(target=MON.run, daemon=True, name="monitor")
    t.start()
    if CFG["integrity"]:
        threading.Thread(target=SCANNER.run, daemon=True, name="integrity").start()
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


templates.env.filters.update(dt=fmt_dt, bytes=fmt_bytes, dur=fmt_dur, n=fmt_int, ago=ago)
templates.env.globals.update(base=CFG["base"], END_REASONS=END_REASONS, decode_exit=decode_exit,
                             tzname=str(DISPLAY_TZ))


def ctx(request, **kw):
    now = datetime.now(timezone.utc)
    with MON.lock:
        st = MON.tail.state
        h = header(st, MON.albums, MON.disk, now)
        kw.update(ir=SCANNER.report, ip=SCANNER.progress, scanner=SCANNER,
                  integrity_on=CFG["integrity"])
        kw.update(request=request, h=h, now=now, st=st, mon=MON, tail=MON.tail,
                  albums_error=MON.albums_error, disk=MON.disk,
                  bad_events=sum(1 for e in st.events if e.severity == "bad"))
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
        texts = [i.text for i in ac.issues if i.severity == sev]
        r.flags.append((sev, "disk check: " + "; ".join(texts[:3]) + ("…" if len(texts) > 3 else "")))
    return rows


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with MON.lock:
        st = MON.tail.state
        rows = album_rows(st, MON.albums, MON.disk, current_set())
        recent = [e for e in reversed(st.events) if e.severity != "info"][:15]
    attach_checks(rows)
    flagged = [r for r in rows if r.severity in ("bad", "warn")]
    return templates.TemplateResponse(request, "index.html", ctx(
        request, rows=rows, recent=recent, flagged=flagged, page="albums"))


@app.get("/album/{set_id}", response_class=HTMLResponse)
def album(request: Request, set_id: str):
    with MON.lock:
        st = MON.tail.state
        rows = album_rows(st, MON.albums, MON.disk, current_set())
        row = next((r for r in rows if r.set_id == set_id), None)
        jobs = [j for j in st.jobs if j.set_id == set_id]
        events = [e for e in st.events if e.set_id == set_id]
        d = MON.disk.albums.get(set_id) if MON.disk else None
    rep = SCANNER.report
    ac = rep.albums.get(set_id) if rep else None
    if row is None and ac is not None:        # on disk only (not in albums.txt nor the log)
        row = AlbumRow(None, set_id, ac.username, ac.title,
                       ac.folders[0] if ac.folders else None, "unlisted")
    if row is None:
        raise HTTPException(404, "unknown album")
    attach_checks([row], flags=False)   # details are shown in the disk-check section
    return templates.TemplateResponse(request, "album.html", ctx(
        request, row=row, jobs=jobs, events=list(reversed(events))[:300], d=d, ac=ac,
        page="albums"))


@app.get("/integrity", response_class=HTMLResponse)
def integrity_page(request: Request):
    rep = SCANNER.report
    albums = sorted(rep.albums.values(), key=lambda a: (
        {"problems": 0, "warnings": 1, "scanning": 2, "in progress": 3, "verified": 4}[a.verdict],
        a.username or "", a.title or "", a.set_id)) if rep else []
    return templates.TemplateResponse(request, "integrity.html", ctx(
        request, albums=albums, page="integrity"))


@app.post("/integrity/rescan")
def integrity_rescan():
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


@app.get("/events", response_class=HTMLResponse)
def events(request: Request, kind: str = "", sev: str = "", limit: int = 500):
    with MON.lock:
        st = MON.tail.state
        evs = [e for e in reversed(st.events)
               if (not kind or e.kind == kind) and (not sev or e.severity == sev)][:min(limit, 5000)]
        counts = event_counts(st)
    return templates.TemplateResponse(request, "events.html", ctx(
        request, evs=evs, counts=counts, kind=kind, sev=sev, page="events"))


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
