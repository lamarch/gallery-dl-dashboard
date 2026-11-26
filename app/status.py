"""Derive the live header and per-album rows from log + albums.txt + disk."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Optional

from .albums import merge_albums, non_album_lines
from .logparse import END_OK, END_REASONS, LogState, decode_exit

STALL_GRACE = timedelta(minutes=3)      # after a scheduled sleep/wait should end
STALL_ACTIVE = timedelta(minutes=5)     # without any scheduled sleep
EXTRACTOR_PAUSE = 3600                  # sleep-extractor from the config
DEFAULT_SPP = 25.0                      # seconds per photo before we have data


@dataclass
class AlbumRow:
    pos: Optional[int]
    set_id: Optional[str]
    account: Optional[str]
    title: Optional[str]
    folder: Optional[str]
    state: str                          # current|done|pending|unlisted
    passes: int = 0
    end_reason: Optional[str] = None
    end_label: Optional[str] = None
    seen: int = 0
    downloaded: int = 0
    skipped: int = 0
    missing: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    retries: int = 0
    waits: int = 0
    errors: int = 0
    log_bytes: int = 0
    disk_media: Optional[int] = None
    disk_bytes: Optional[int] = None
    flags: list = field(default_factory=list)   # (severity, text)
    last_seen: Optional[datetime] = None
    check: object = None                        # integrity.AlbumCheck, when scanned
    fb_count: Optional[int] = None              # photos shown by Facebook (imported page)
    fb_approx: bool = False
    log_full: Optional[int] = None              # photos seen by the last pass that reached the end
    expected_ok: Optional[bool] = None          # Facebook = last full pass = on disk
    eta: object = None                          # Eta for the current / pending albums
    first_start: Optional[datetime] = None      # first pass in the log
    last_end: Optional[datetime] = None         # end of the last pass (None while running)
    running: bool = False
    time_total: float = 0.0                     # all passes, seconds (pauses included)
    time_pause: float = 0.0                     # the 1 h pauses before the album
    time_work: float = 0.0                      # downloading (pauses and 429 waits excluded)

    @property
    def severity(self) -> str:
        sev = {s for s, _ in self.flags}
        return "bad" if "bad" in sev else "warn" if "warn" in sev else "ok"


def album_rows(state: LogState, albums: list, disk, current_set: Optional[str],
               now: Optional[datetime] = None, fb: Optional[dict] = None,
               fb_titles: Optional[dict] = None) -> list[AlbumRow]:
    now = now or datetime.now(timezone.utc)
    fb, fb_titles = fb or {}, fb_titles or {}
    albums = merge_albums(albums)
    jobs_by_set: dict = {}
    for j in state.jobs:
        jobs_by_set.setdefault(j.set_id, []).append(j)
    disk_albums = disk.albums if disk else {}
    rows, listed = [], set()

    def build(pos, set_id, listed_done):
        d = disk_albums.get(set_id) if set_id else None
        jobs = jobs_by_set.get(set_id, [])
        if set_id == current_set:
            st = "current"
        elif listed_done is None:
            st = "unlisted"
        else:
            st = "done" if listed_done else "pending"
        r = AlbumRow(pos, set_id,
                     (d.username or d.account_folder) if d else None,
                     fb_titles.get(set_id) or ((d.title or d.folder) if d else None),
                     d.folder if d else None, st, passes=len(jobs))
        if set_id in fb:
            r.fb_count, r.fb_approx = fb[set_id]
        have, missing, failed = set(), {}, {}
        downloaded_ids = set()
        for j in jobs:
            for pid, p in j.photos.items():
                r.retries += p.url_retries + p.download_retries
                if p.status in ("downloaded", "skipped"):
                    have.add(pid)
                    # only photos the log saw before the disk snapshot can be
                    # expected in it (the snapshot is up to a minute old)
                    if p.status == "downloaded" and (
                            disk is None or p.last_ts.timestamp() < disk.taken - 60):
                        downloaded_ids.add(pid)
                elif p.status == "missing":
                    missing[pid] = j
                elif p.status in ("failed", "unknown"):
                    failed[pid] = j
            r.waits += j.waits_429
            r.errors += j.errors
            r.log_bytes += j.bytes
        if jobs:
            r.first_start = jobs[0].start
            for j in jobs:
                dur = j.durations(now)
                r.time_total += dur["total"]
                r.time_pause += dur["pause"]
                r.time_work += dur["work"]
            _, last_end, r.running = jobs[-1].span(now)
            r.last_end = None if r.running else last_end
            last = jobs[-1]
            r.end_reason = last.end_reason
            r.end_label = END_REASONS.get(last.end_reason, last.end_reason)
            r.last_seen = last.end or last.start
            c = last.counts()
            r.seen = len(last.photos)
            r.downloaded = sum(1 for p in last.photos.values()
                               if p.status == "downloaded" or (p.status == "pending" and p.cdn_ok))
            r.skipped = c["skipped"]
        on_disk = d.media_ids if d else set()
        r.missing = sorted(pid for pid in missing if pid not in have and pid not in on_disk)
        r.failed = sorted(pid for pid in failed if pid not in have and pid not in on_disk)
        if d:
            r.disk_media, r.disk_bytes = d.media, d.bytes
        if jobs and jobs[-1].end_reason in END_OK:
            r.log_full = len(jobs[-1].photos)
        # compare only albums that were downloaded (done, or a pass in the log that
        # is over); pending / current ones just show the expected count
        if r.fb_count is not None and st not in ("current", "pending") and (jobs or d):
            counts = [c for c in (r.log_full, len(d.media_ids) if d else None) if c is not None]
            if counts:
                r.expected_ok = all(c == r.fb_count for c in counts) or (
                    r.fb_approx and all(abs(c - r.fb_count) <= max(1, r.fb_count * 0.05) for c in counts))
            n_disk = len(d.media_ids) if d else 0
            if n_disk < r.fb_count and not (r.fb_approx and r.fb_count - n_disk <= max(1, r.fb_count * 0.05)):
                r.flags.append(("bad", f"Facebook shows {r.fb_count} photo(s), {n_disk} on disk"))
            elif d and n_disk > r.fb_count and not r.fb_approx:
                r.flags.append(("info", f"{n_disk - r.fb_count} more on disk than Facebook shows"))
            if r.log_full is not None and r.log_full != r.fb_count and not r.fb_approx:
                r.flags.append(("warn", f"the last complete pass saw {r.log_full} photo(s), Facebook shows "
                                        f"{r.fb_count} (album changed on Facebook since?)"))

        # ---- flags -------------------------------------------------------------
        if r.missing:
            r.flags.append(("bad", f"{len(r.missing)} photo(s) skipped by gallery-dl "
                                   f"(no download URL) and not on disk"))
        if r.failed:
            r.flags.append(("bad", f"{len(r.failed)} download(s) failed and not on disk"))
        if jobs and jobs[-1].end_reason in ("blocked", "login", "error"):
            r.flags.append(("bad", f"last pass: {r.end_label}"))
        if jobs and jobs[-1].end_reason == "jump":
            r.flags.append(("warn", "ended by the 'jump' heuristic: compare the count with Facebook"))
        if st == "done" and jobs and jobs[-1].end_reason not in END_OK | {"jump"}:
            r.flags.append(("warn", f"marked done in albums.txt, but last pass: {r.end_label}"))
        if d and downloaded_ids:
            lost = downloaded_ids - on_disk
            if lost and st != "current":
                r.flags.append(("bad", f"{len(lost)} photo(s) downloaded per the log but not found on disk"))
        if d and d.part and st != "current":
            r.flags.append(("warn", f"{d.part} unfinished .part file(s)"))
        if d and d.media_without_json:
            r.flags.append(("warn", f"{d.media_without_json} file(s) without JSON metadata"))
        if st == "done" and not jobs:
            r.flags.append(("info", "done before the current log started"))
        if st in ("done",) and d is None:
            r.flags.append(("bad", "marked done but no folder on disk"))
        return r

    for a in albums:
        listed.add(a.set_id)
        rows.append(build(a.pos, a.set_id, a.done))
    for set_id in jobs_by_set:
        if set_id not in listed:
            rows.append(build(None, set_id, None))
    etas = plan_queue(state, albums, disk, fb, now)[0]
    for r in rows:
        r.eta = etas.get(r.set_id)
    return rows


# --------------------------------------------------------------------------
def seconds_per_photo(state: LogState) -> Optional[float]:
    diffs = []
    for s in reversed(state.sessions):
        for j in reversed(s.jobs):
            ts = sorted(p.first_ts for p in j.photos.values())
            diffs.extend((b - a).total_seconds() for a, b in zip(ts, ts[1:]))
            if len(diffs) >= 300:
                break
        if len(diffs) >= 300:
            break
    return mean(diffs[:300]) if diffs else None


@dataclass
class Eta:
    start: datetime             # expected start (the pass start for the current album)
    finish: datetime            # expected end
    size: float                 # photos expected
    from_fb: bool               # size comes from Facebook (else: average album size)


def plan_queue(state: LogState, albums: list, disk, fb: Optional[dict], now: datetime):
    """Expected start/end of the current album and of every pending album, in
    albums.txt order: 1 h pause + photos x measured seconds per photo.
    -> (etas {set_id: Eta}, end of the queue, seconds per photo, average size)"""
    albums = merge_albums(albums)
    spp = seconds_per_photo(state) or DEFAULT_SPP
    done_sizes = [disk.albums[a.set_id].media for a in albums
                  if a.done and disk and a.set_id in disk.albums and disk.albums[a.set_id].media]
    if not done_sizes and disk:
        done_sizes = [d.media for d in disk.albums.values() if d.media]
    avg = mean(done_sizes) if done_sizes else 50.0
    fb = fb or {}

    def size(sid):
        c = fb.get(sid, (None, False))[0]
        return (c, True) if c is not None else (avg, False)

    s = state.session
    job = s.current_job if s is not None and s.end is None else None
    act = state.activity
    etas, t = {}, now
    if job is not None and job.set_id:
        n, from_fb = size(job.set_id)
        if act and act.reason == "extractor" and act.until and act.until > now:
            left = (act.until - now).total_seconds() + n * spp
        else:
            left = max(n - len(job.photos), 0) * spp
        t = now + timedelta(seconds=left)
        etas[job.set_id] = Eta(job.start, t, n, from_fb)
    for a in albums:
        if a.done or a.set_id in etas or not a.set_id:
            continue
        n, from_fb = size(a.set_id)
        start = t
        t = t + timedelta(seconds=EXTRACTOR_PAUSE + n * spp)
        etas[a.set_id] = Eta(start, t, n, from_fb)
    return etas, t, spp, avg


@dataclass
class Header:
    state: str
    label: str
    detail: str = ""
    until: Optional[datetime] = None
    since: Optional[datetime] = None
    last_line: Optional[datetime] = None
    account: Optional[str] = None
    title: Optional[str] = None
    set_id: Optional[str] = None
    pos: Optional[int] = None
    total: int = 0
    done: int = 0
    album_seen: int = 0
    album_downloaded: int = 0
    album_skipped: int = 0
    album_missing: int = 0
    album_bytes: int = 0
    album_errors: int = 0
    album_retries: int = 0
    album_expected: Optional[int] = None
    photos_last_hour: int = 0
    waits_24h: int = 0
    problems_24h: int = 0
    session_no: Optional[int] = None
    session_start: Optional[datetime] = None
    archive: Optional[bool] = None
    disk_media: Optional[int] = None
    disk_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    fs_bytes: Optional[int] = None
    downloaded_bytes: int = 0
    session_bytes: int = 0
    eta_seconds: Optional[float] = None
    eta_pause_seconds: Optional[float] = None
    spp: Optional[float] = None
    avg_album: Optional[float] = None
    eta_from_fb: int = 0              # remaining albums whose size comes from Facebook
    # totals for the albums in albums.txt
    photos_expected: Optional[float] = None      # Facebook counts (+ average size when unknown)
    photos_have: int = 0                         # on disk for those albums
    albums_count_unknown: int = 0                # pending albums without a Facebook count (estimated)
    photos_remaining: Optional[float] = None     # sum over albums of max(expected - on disk, 0)
    lines_not_albums: int = 0                    # albums.txt lines that are not albums (left out)
    bytes_per_photo: Optional[float] = None
    est_total_bytes: Optional[float] = None      # expected photos x average photo size
    est_remaining_bytes: Optional[float] = None
    space_warning: bool = False                  # remaining download may not fit
    album_eta_at: Optional[datetime] = None      # expected end of the current album
    album_eta_seconds: Optional[float] = None


def header(state: LogState, albums: list, disk, now: Optional[datetime] = None,
           acked=None, fb: Optional[dict] = None) -> Header:
    now = now or datetime.now(timezone.utc)
    raw_albums = albums
    albums = merge_albums(albums)
    s = state.session
    if s is None:
        return Header("nodata", "No log data yet")
    job = s.current_job
    act = state.activity
    h = Header("downloading", "Downloading", last_line=state.last_ts)
    h.session_no = s.idx + 1
    h.session_start = s.start
    h.archive = s.archive
    h.downloaded_bytes = state.total_bytes
    h.session_bytes = sum(j.bytes for j in s.jobs)

    # ---- state ------------------------------------------------------------
    if s.end is not None:
        if s.exit_code is not None:
            ok = s.exit_code == 0
            h.state = "finished" if ok else "finished_err"
            h.label = "Finished" if ok else f"Finished with errors (exit code {s.exit_code})"
            h.detail = ", ".join(decode_exit(s.exit_code))
        else:
            h.state, h.label = "stalled", "Stopped"
        h.since = s.end
    else:
        last_job = s.jobs[-1] if s.jobs else None
        if last_job is not None and last_job.end_reason in ("blocked", "login"):
            h.state = "blocked"
            h.label = "Blocked by Facebook" if last_job.end_reason == "blocked" else "Login required"
        if act is not None and act.until is not None and act.until > now:
            if act.kind == "wait":
                h.state, h.label = "ratelimit", "Rate-limited, waiting"
            elif act.reason == "extractor":
                h.state, h.label = "pause", "Pause before the album"
            elif h.state != "blocked":
                h.state, h.label = "sleeping", "Working"
                h.detail = f"sleeping ({act.reason})"
            h.until = act.until
            h.since = act.start
        quiet_limit = ((act.until + STALL_GRACE) if act and act.until else
                       (state.last_ts + STALL_ACTIVE))
        if now > quiet_limit:
            h.state, h.label = "stalled", "Stalled: no log activity"
            h.detail = "the log has been quiet longer than any scheduled pause"
            h.since = state.last_ts

    # ---- album ------------------------------------------------------------
    cur = job or (s.jobs[-1] if s.jobs else None)
    if cur is not None:
        h.set_id = cur.set_id
        if fb and cur.set_id in fb:
            h.album_expected = fb[cur.set_id][0]
        d = disk.albums.get(cur.set_id) if disk and cur.set_id else None
        if d:
            h.account = d.username or d.account_folder
            h.title = d.title or d.folder
        c = cur.counts()
        h.album_seen = len(cur.photos)
        h.album_downloaded = sum(1 for p in cur.photos.values()
                                 if p.status == "downloaded" or (p.status == "pending" and p.cdn_ok))
        h.album_skipped = c["skipped"]
        h.album_missing = c["missing"]
        h.album_bytes = cur.bytes
        h.album_errors = cur.errors
        h.album_retries = sum(p.url_retries + p.download_retries for p in cur.photos.values())
    h.total = len(albums)
    h.done = sum(1 for a in albums if a.done)
    for a in albums:
        if h.set_id and a.set_id == h.set_id:
            h.pos = a.pos

    # ---- activity stats -----------------------------------------------------
    hour, day = now - timedelta(hours=1), now - timedelta(hours=24)
    h.photos_last_hour = sum(1 for j in s.jobs for p in j.photos.values() if p.first_ts >= hour)
    for e in reversed(state.events):
        if e.ts < day:
            break
        if e.kind == "wait_429":
            h.waits_24h += 1
        elif e.severity == "bad" and not (acked and acked(e)):
            h.problems_24h += 1

    # ---- disk -------------------------------------------------------------
    if disk:
        h.disk_media, h.disk_bytes = disk.total_media, disk.total_bytes
        h.free_bytes, h.fs_bytes = disk.free_bytes, disk.fs_bytes

    # ---- expected totals and size (one entry per album in albums.txt) --------
    h.lines_not_albums = non_album_lines(raw_albums)
    if albums:
        fbc = fb or {}

        def have_of(sid):
            d = disk.albums.get(sid) if disk else None
            return len(d.media_ids) if d else 0

        done_sizes = [have_of(a.set_id) for a in albums if a.done and have_of(a.set_id)]
        avg_album = mean(done_sizes) if done_sizes else None
        expected = have = remaining = 0.0
        unknown = 0
        for a in albums:
            n_disk = have_of(a.set_id)
            c = fbc.get(a.set_id, (None, False))[0]
            if c is None:
                if a.done:
                    c = n_disk                 # finished, no Facebook count: what is on disk
                else:
                    unknown += 1               # estimated at the average album size
                    c = max(avg_album or 0, n_disk)
            expected += c
            have += n_disk
            remaining += max(c - n_disk, 0)    # per album: extra photos never hide missing ones
        h.albums_count_unknown = unknown
        if expected:
            h.photos_expected, h.photos_have, h.photos_remaining = expected, int(have), remaining
        if disk and disk.total_media:
            h.bytes_per_photo = disk.total_bytes / disk.total_media
        if h.photos_expected is not None and h.bytes_per_photo:
            h.est_total_bytes = h.photos_expected * h.bytes_per_photo
            h.est_remaining_bytes = remaining * h.bytes_per_photo
            if disk and disk.free_bytes is not None:
                h.space_warning = h.est_remaining_bytes > disk.free_bytes * 0.9

    # ---- ETA (same queue as the albums table) ----------------------------------
    if h.state not in ("finished", "finished_err") and albums:
        etas, end, spp, avg = plan_queue(state, albums, disk, fb, now)
        h.eta_seconds = max(0.0, (end - now).total_seconds())
        remaining = [a for a in albums if not a.done and a.set_id != h.set_id]
        h.eta_pause_seconds = len(remaining) * EXTRACTOR_PAUSE
        h.eta_from_fb = sum(1 for a in remaining if etas.get(a.set_id) and etas[a.set_id].from_fb)
        h.spp, h.avg_album = spp, avg
        cur_eta = etas.get(h.set_id) if job is not None else None
        if cur_eta:
            h.album_eta_at = cur_eta.finish
            h.album_eta_seconds = max(0.0, (cur_eta.finish - now).total_seconds())
    return h


def event_counts(state: LogState) -> Counter:
    return Counter(e.kind for e in state.events)


_RANK = {"downloaded": 3, "skipped": 3, "missing": 1, "failed": 1, "unknown": 1, "pending": 0}


def log_snapshot(state: LogState) -> dict:
    """Per album: what every pass in the log saw, for the disk check.

    photos: {photo_id: (best status over all passes, last seen as epoch)}
    last_ids: photo IDs of the last pass, in order.
    """
    out: dict = {}
    for j in state.jobs:
        if not j.set_id:
            continue
        a = out.setdefault(j.set_id, {"passes": 0, "photos": {}, "last_end": None, "last_ids": []})
        a["passes"] += 1
        a["last_end"] = j.end_reason
        a["last_ids"] = list(j.photos)
        for pid, p in j.photos.items():
            st = "downloaded" if (p.status == "pending" and p.cdn_ok) else p.status
            ts = p.last_ts.timestamp()
            prev = a["photos"].get(pid)
            if prev is None or _RANK[st] >= _RANK[prev[0]]:
                a["photos"][pid] = (st, max(ts, prev[1]) if prev else ts)
            else:
                a["photos"][pid] = (prev[0], max(ts, prev[1]))
    return out
