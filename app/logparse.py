"""Incremental parser for gallery-dl log files (Facebook extractor).

Built against gallery-dl 1.32.21 with
    output.logfile.format = "[{asctime}] [{name}][{levelname}] {message}"
and --verbose / level "debug".

Model:  Session (one gallery-dl process)  ->  Job (one input URL = one album)
        ->  Photo (one /photo/?fbid=... page request chain)
plus Events (every warning/error and notable info) and sleep/wait accounting.

Privacy: CDN URLs (signed tokens) are never stored; facebook.com URLs are
reduced to path + fbid/set parameters.
"""
from __future__ import annotations

import os
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlsplit

# --------------------------------------------------------------------------
# Line grammar
# --------------------------------------------------------------------------
RE_LINE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[([^\]]+)\]\[(debug|info|warning|error|critical)\] ?(.*)$")
RE_BAD_FORMAT = re.compile(r"^\[%\(asctime\)s\]")

RE_VERSION = re.compile(r"^Version (\S+)")
RE_STARTING = re.compile(r"^Starting (\w+) for '(.*)'$")
RE_RESTARTING = re.compile(r"^Restarting '(.*)'$")
RE_USING_EXTR = re.compile(r"^Using (\w+) for '(.*)'$")
RE_SLEEP = re.compile(r"^Sleeping (\d+(?:\.\d+)?) seconds \((\w+)\)$")
RE_WAIT = re.compile(r"^Waiting for (.+?) until (\d\d:\d\d:\d\d) \((.*)\)$")
RE_NO_URL_RETRY = re.compile(
    r"^Failed to find photo download URL for (\S+?)\. Retrying in (\d+(?:\.\d+)?) seconds\.$")
RE_NO_URL_SKIP = re.compile(r"^Failed to find photo download URL for (\S+?)\. Skipping\.$")
RE_JUMP = re.compile(r"^Detected jump to the beginning of the set\. \((\S+) -> (\S+)\)$")
RE_HTTP_RETRY = re.compile(r"^'(\d{3}) ([^']*)' for '([^']*)' \((\d+)/(\d+)\)$")
RE_HTTP_FAIL = re.compile(r"^'(\d{3}) ([^']*)' for '([^']*)'$")
RE_GENERIC_RETRY = re.compile(r"^(.*) \((\d+)/(\d+)\)$")
RE_ADJUST = re.compile(r"^Adjusting filename extension of '(.*)' to '(\w+)'$")
RE_FAILED_DL = re.compile(r"^Failed to download (.*)$")
RE_ARCHIVE = re.compile(r"^Using download archive '(.*)'$")
RE_ARCHIVE_FAIL = re.compile(r"^Failed to open download archive at '(.*)' \((.*)\)$")
RE_WRAPPER_EXIT = re.compile(r"^gallery-dl exited with code (\d+)$")
RE_URLLIB_RESP = re.compile(
    r'^(https?)://([^: ]+)(?::(\d+))? "(\w+) (\S+) HTTP/[\d.]+" (\d{3}) (\S+)$')
RE_FBID = re.compile(r"fbid=(\d+)")
RE_SET_IN_URL = re.compile(r"[?&]set=([^&\s]+?)(?:\.?$|&|\s)")
RE_URL = re.compile(r"https?://[^\s'\"<>]+")
RE_FILE_ID = re.compile(r"_(\d{6,})\.\w+$")

IGNORED_DEBUG = (
    re.compile(r"^Python "), re.compile(r"^requests "), re.compile(r"^Configuration Files"),
    re.compile(r"^cookies: "), re.compile(r"^Active postprocessor modules"),
    re.compile(r"^Starting new HTTPS? connection"), re.compile(r"^Resetting dropped connection"),
    re.compile(r"^Resuming download at byte"), re.compile(r"^Unable to consume response body"),
    re.compile(r"^Got empty profile photos page"), re.compile(r"^Found a followup in comments"),
    re.compile(r"^Unable to resume partial download"), re.compile(r"^Failed to extract user data"),
)

EXIT_BITS = {
    1: "unexpected error", 4: "extraction/download error", 8: "challenge (captcha)",
    16: "login required", 32: "input error", 64: "unsupported URL",
    128: "OS/network error",
}


def decode_exit(code: int) -> list[str]:
    if code == 0:
        return ["success"]
    return [v for k, v in EXIT_BITS.items() if code & k] or [f"code {code}"]


LOG_TZ = timezone.utc   # timezone of gallery-dl's {asctime} (container local time)


def set_log_tz(tz):
    global LOG_TZ
    LOG_TZ = tz


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOG_TZ)


def parse_wait_duration(s: str) -> Optional[float]:
    """'15 minutes' / '2h 0min' / '45 seconds' -> seconds (as logged by wait())."""
    m = re.match(r"^(\d+)h (\d+)min$", s)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.match(r"^(\d+) minutes?$", s)
    if m:
        return int(m.group(1)) * 60
    m = re.match(r"^(\d+) seconds?$", s)
    if m:
        return int(m.group(1))
    return None


def sanitize_url(url: str) -> str:
    """Drop secrets: CDN URLs entirely, other query strings except fbid/set."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    host = parts.hostname or ""
    if host.endswith("fbcdn.net"):
        return "<cdn>"
    if host.endswith("facebook.com"):
        q = parse_qs(parts.query)
        keep = [f"{k}={q[k][0]}" for k in ("fbid", "set") if k in q]
        return f"{parts.path or '/'}" + (("?" + "&".join(keep)) if keep else "")
    return f"{parts.scheme}://{host}{parts.path}"


def sanitize_text(text: str) -> str:
    return RE_URL.sub(lambda m: sanitize_url(m.group(0)), text)


def set_id_from_url(url: str) -> Optional[str]:
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        q = {}
    if "set" in q and q["set"][0]:
        return q["set"][0]
    m = RE_SET_IN_URL.search(url)
    return m.group(1) if m else None


def template_of(msg: str) -> str:
    """Normalize a message into a privacy-safe template for 'unrecognized'."""
    t = RE_URL.sub("<URL>", msg)
    t = re.sub(r"'[^']*'", "<Q>", t)
    t = re.sub(r'"[^"]*"', "<S>", t)
    t = re.sub(r"\d+(?:\.\d+)?", "N", t)
    return t[:200]


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
@dataclass
class Event:
    ts: datetime
    level: str
    logger: str
    kind: str
    message: str
    session: int
    job: Optional[int] = None
    set_id: Optional[str] = None
    photo_id: Optional[str] = None
    details: list = field(default_factory=list)
    key: str = ""             # stable ID (content + occurrence), for acknowledgements

    @property
    def severity(self) -> str:
        if self.kind in ("photo_missing", "blocked", "login", "download_failed",
                         "unexpected", "archive_failed", "input_update_failed",
                         "challenge", "session_interrupted"):
            return "bad"
        if self.level in ("warning", "error") or self.kind in ("wait_429", "jump", "no_results"):
            return "warn"
        return "info"


@dataclass
class Photo:
    id: str
    first_ts: datetime
    last_ts: datetime
    page_requests: int = 0
    url_retries: int = 0          # "Failed to find photo download URL ... Retrying"
    download_retries: int = 0
    download_started: bool = False
    cdn_ok: bool = False
    status: str = "pending"       # pending|downloaded|skipped|missing|failed|unknown
    bytes: int = 0
    filename: Optional[str] = None


@dataclass
class Job:
    idx: int
    session: int
    url: str                      # sanitized
    set_id: Optional[str]
    start: datetime
    jobtype: str = "DownloadJob"
    extractor: Optional[str] = None
    end: Optional[datetime] = None
    end_reason: str = "running"   # see END_REASONS
    archive: Optional[bool] = None
    photos: "OrderedDict[str, Photo]" = field(default_factory=OrderedDict)
    events: list = field(default_factory=list)
    errors: int = 0
    warnings: int = 0
    waits_429: int = 0
    wait_seconds: float = 0.0
    bytes: int = 0
    current: Optional[Photo] = None
    sleep_seconds: Counter = field(default_factory=Counter)   # by reason, in this pass
    last_ts: Optional[datetime] = None
    ended_at: Optional[datetime] = None   # end-of-album message (pass not closed yet)

    def span(self, now: Optional[datetime] = None):
        """(start, end, running): a running pass ends 'now'."""
        if self.end is not None:
            return self.start, self.end, False
        if self.ended_at is not None:
            return self.start, self.ended_at, False
        return self.start, (now or self.last_ts or self.start), True

    def durations(self, now: Optional[datetime] = None) -> dict:
        """Seconds: total, pause (sleep-extractor before the album), rate-limit
        waits, and work (total minus pause and waits)."""
        start, end, _ = self.span(now)
        total = max(0.0, (end - start).total_seconds())
        pause = min(total, self.sleep_seconds.get("extractor", 0.0))
        waits = min(total - pause, self.wait_seconds)
        return {"total": total, "pause": pause, "waits": waits,
                "work": max(0.0, total - pause - waits)}

    def counts(self) -> Counter:
        return Counter(p.status for p in self.photos.values())


END_REASONS = {
    "running": "running",
    "complete": "reached the end of the album",
    "complete_loop": "reached the end (loop detected)",
    "jump": "stopped by 'jump to beginning' heuristic",
    "blocked": "temporarily blocked by Facebook",
    "login": "login required (cookies expired?)",
    "error": "stopped by an error",
    "no_results": "no results",
    "interrupted": "interrupted (crash, power cut or stop)",
    "unknown": "ended without an end-of-album message",
}
END_OK = {"complete", "complete_loop"}


@dataclass
class Session:
    idx: int
    start: datetime
    version: Optional[str] = None
    last_ts: Optional[datetime] = None
    end: Optional[datetime] = None
    exit_code: Optional[int] = None
    end_reason: str = "running"   # running|exited|interrupted
    jobs: list = field(default_factory=list)
    sleep_seconds: Counter = field(default_factory=Counter)
    sleep_counts: Counter = field(default_factory=Counter)
    wait_seconds: float = 0.0
    wait_count: int = 0
    archive: Optional[bool] = None
    lines: int = 0

    def durations(self, now: Optional[datetime] = None) -> dict:
        """Seconds: total, pauses (sleep-extractor between albums), rate-limit
        waits, active (total minus pauses and waits)."""
        end = self.end or (now if self.end_reason == "running" and now else self.last_ts) or self.start
        total = max(0.0, (end - self.start).total_seconds())
        pause = min(total, self.sleep_seconds.get("extractor", 0.0))
        waits = min(total - pause, self.wait_seconds)
        return {"total": total, "pause": pause, "waits": waits,
                "active": max(0.0, total - pause - waits)}

    @property
    def current_job(self) -> Optional[Job]:
        return self.jobs[-1] if self.jobs and self.jobs[-1].end is None else None


@dataclass
class Activity:
    """What gallery-dl was doing at the last log line."""
    kind: str                     # sleep|wait|request|download|start|other
    start: datetime
    until: Optional[datetime] = None
    reason: Optional[str] = None


class LogState:
    def __init__(self):
        self.sessions: list[Session] = []
        self.jobs: list[Job] = []
        self.events: list[Event] = []
        self.unrecognized: Counter = Counter()
        self.unrecognized_example_ts: dict = {}
        self._event_seen: dict = {}
        self.bad_format_lines = 0
        self.orphan_lines = 0
        self.lines = 0
        self.first_ts: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None
        self.activity: Optional[Activity] = None
        self.total_bytes = 0
        self._last_record: Optional[tuple] = None   # (event or None)
        self._last_event: Optional[Event] = None
        self._pending_tb: list = []
        self._pending_tb_ts: Optional[datetime] = None

    # ---------------------------------------------------------------- helpers
    @property
    def session(self) -> Optional[Session]:
        return self.sessions[-1] if self.sessions else None

    @property
    def job(self) -> Optional[Job]:
        s = self.session
        return s.current_job if s else None

    def _event(self, ts, level, logger, kind, message, photo_id=None) -> Event:
        s = self.session
        j = self.job
        ev = Event(ts, level, logger, kind, sanitize_text(message),
                   s.idx if s else -1, j.idx if j else None,
                   j.set_id if j else None, photo_id)
        if self._pending_tb and (ts - self._pending_tb_ts).total_seconds() <= 5:
            ev.details.extend(self._pending_tb)
        self._pending_tb = []
        self._keyed(ev)
        self.events.append(ev)
        if j is not None:
            j.events.append(ev)
            if level == "error":
                j.errors += 1
            elif level == "warning":
                j.warnings += 1
        self._last_event = ev
        return ev

    def _finish_photo(self, job: Job):
        p = job.current
        if p is None:
            return
        if p.status == "pending":
            if p.cdn_ok:
                p.status = "downloaded"
            elif p.download_started:
                p.status = "unknown"   # download began, no success seen
            else:
                p.status = "skipped"   # page loaded, no download: already had it
        job.current = None

    def _end_job(self, ts, reason=None):
        j = self.job
        if j is None:
            return
        self._finish_photo(j)
        j.end = ts
        if reason is not None and j.end_reason == "running":
            j.end_reason = reason
        if j.end_reason == "running":
            j.end_reason = "unknown"

    def _set_end(self, reason, ts=None):
        j = self.job
        if j is not None and (j.end_reason == "running" or reason in ("blocked", "login")):
            j.end_reason = reason
            if ts is not None:
                j.ended_at = ts          # the album is over, even if the pass stays open

    def _end_session(self, ts, reason, exit_code=None):
        s = self.session
        if s is None or s.end is not None:
            return
        self._end_job(s.last_ts or ts, "interrupted" if reason == "interrupted" else None)
        s.end = s.last_ts or ts if reason == "interrupted" else ts
        s.end_reason = reason
        s.exit_code = exit_code

    def _ensure_session(self, ts):
        if self.session is None or self.session.end is not None:
            self.sessions.append(Session(idx=len(self.sessions), start=ts))

    def _keyed(self, ev: "Event"):
        """Stable ID: same log -> same IDs after a restart (the log is re-read
        in the same order); identical events in the same second are numbered."""
        import hashlib
        base = hashlib.sha1("|".join((ev.ts.isoformat(), ev.kind, ev.set_id or "", ev.photo_id or "",
                                      ev.message)).encode("utf-8", "replace")).hexdigest()[:16]
        n = self._event_seen.get(base, 0)
        self._event_seen[base] = n + 1
        ev.key = f"{base}-{n}"

    # ------------------------------------------------------------------ feed
    def feed_line(self, line: str):
        self.lines += 1
        line = line.rstrip("\r\n")
        if not line:
            return
        m = RE_LINE.match(line)
        if not m:
            if RE_BAD_FORMAT.match(line):
                self.bad_format_lines += 1
            elif self._last_record == "tb":
                if len(self._pending_tb) < 80:
                    self._pending_tb.append(sanitize_text(line))
            elif self._last_event is not None and self._last_record == "event":
                if len(self._last_event.details) < 60:
                    self._last_event.details.append(sanitize_text(line))
            elif self._last_record is not None:
                pass   # continuation of an unstored (debug) record
            else:
                self.orphan_lines += 1
            return
        ts = parse_ts(m.group(1))
        logger, level, msg = m.group(2), m.group(3), m.group(4)
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts
        self._last_record = "other"
        self._last_event = None
        handled = self._dispatch(ts, logger, level, msg)
        if self.session is not None:
            self.session.last_ts = ts
            self.session.lines += 1
            j = self.session.current_job
            if j is not None:
                j.last_ts = ts
        if not handled:
            if level in ("warning", "error", "critical"):
                kind = "other_" + ("error" if level != "warning" else "warning")
                self._event(ts, level, logger, kind, msg)
                self._last_record = "event"
            else:
                key = f"[{logger}][{level}] {template_of(msg)}"
                self.unrecognized[key] += 1
                self.unrecognized_example_ts.setdefault(key, ts)

    def _dispatch(self, ts, logger, level, msg) -> bool:  # noqa: C901
        if level == "debug" and msg == "":
            # log.traceback(exc): traceback lines follow, and gallery-dl logs
            # the matching error line right AFTER them -> keep them pending.
            self._pending_tb = []
            self._pending_tb_ts = ts
            self._last_record = "tb"
            return True
        # ---- wrapper (compose entrypoint) ---------------------------------
        if logger == "wrapper":
            m = RE_WRAPPER_EXIT.match(msg)
            if m:
                code = int(m.group(1))
                if self.session is None:
                    self._ensure_session(ts)
                self._event(ts, "info" if code == 0 else "warning", logger,
                            "wrapper_exit", f"gallery-dl exited with code {code} "
                            f"({', '.join(decode_exit(code))})")
                self._end_session(ts, "exited", code)
                self.activity = Activity("finished", ts)
                return True
            return False

        # ---- gallery-dl main ------------------------------------------------
        if logger == "gallery-dl":
            m = RE_VERSION.match(msg)
            if m:
                if self.session is not None and self.session.end is None:
                    ev_ts = self.session.last_ts or ts
                    open_job = self.job
                    self._end_session(ts, "interrupted")
                    ev = Event(ev_ts, "warning", "dashboard", "session_interrupted",
                               "gallery-dl stopped without exiting normally "
                               "(crash, power cut or container stop)",
                               self.session.idx,
                               open_job.idx if open_job else None,
                               open_job.set_id if open_job else None)
                    self._keyed(ev)
                    self.events.append(ev)
                    if open_job is not None:
                        open_job.events.append(ev)
                self.sessions.append(Session(idx=len(self.sessions), start=ts, version=m.group(1)))
                self.activity = Activity("start", ts)
                return True
            m = RE_STARTING.match(msg)
            if m:
                self._ensure_session(ts)
                self._end_job(ts)
                url = m.group(2)
                j = Job(idx=len(self.jobs), session=self.session.idx,
                        url=sanitize_url(url), set_id=set_id_from_url(url),
                        start=ts, jobtype=m.group(1))
                self.jobs.append(j)
                self.session.jobs.append(j)
                self.activity = Activity("start", ts)
                return True
            m = RE_RESTARTING.match(msg)
            if m:
                self._event(ts, level, logger, "restart", msg)
                return True
            if level == "debug" and any(r.match(msg) for r in IGNORED_DEBUG):
                return True
            if msg.startswith("Unsupported URL"):
                self._event(ts, level, logger, "unsupported_url", msg)
                self._last_record = "event"
                return True
            return False

        self._ensure_session(ts)
        job = self.job

        # ---- urllib3 responses ----------------------------------------------
        if logger.startswith("urllib3"):
            m = RE_URLLIB_RESP.match(msg)
            if m:
                host, path, status, length = m.group(2), m.group(5), int(m.group(6)), m.group(7)
                self._on_response(ts, host, path, status, length)
                return True
            if level == "debug" and any(r.match(msg) for r in IGNORED_DEBUG):
                return True
            if level in ("warning", "error"):
                self._event(ts, level, logger, "network", msg)
                self._last_record = "event"
                return True
            return False

        # ---- sleeps (any extractor) ------------------------------------------
        m = RE_SLEEP.match(msg)
        if m:
            secs, reason = float(m.group(1)), m.group(2)
            s = self.session
            s.sleep_seconds[reason] += secs
            s.sleep_counts[reason] += 1
            if job is not None:
                job.sleep_seconds[reason] += secs
            self.activity = Activity("sleep", ts, ts + timedelta(seconds=secs), reason)
            if reason == "download" and job is not None and job.current is not None:
                job.current.download_started = True
            return True
        m = RE_WAIT.match(msg)
        if m:
            secs = parse_wait_duration(m.group(1)) or 0
            reason = m.group(3)
            s = self.session
            s.wait_seconds += secs
            s.wait_count += 1
            if job is not None:
                job.waits_429 += 1
                job.wait_seconds += secs
            self.activity = Activity("wait", ts, ts + timedelta(seconds=secs), reason)
            self._event(ts, "warning", logger, "wait_429",
                        f"Waiting {m.group(1)} ({reason})")
            return True

        # ---- extractor (facebook) ---------------------------------------------
        if logger == "facebook":
            m = RE_USING_EXTR.match(msg)
            if m:
                if job is not None:
                    job.extractor = m.group(1)
                return True
            m = RE_ARCHIVE.match(msg)
            if m:
                self.session.archive = True
                if job is not None:
                    job.archive = True
                return True
            m = RE_ARCHIVE_FAIL.match(msg)
            if m:
                self.session.archive = False
                if job is not None:
                    job.archive = False
                self._event(ts, level, logger, "archive_failed", msg)
                return True
            m = RE_NO_URL_RETRY.match(msg)
            if m:
                pid = (RE_FBID.search(m.group(1)) or [None, None])[1]
                p = self._photo(job, pid, ts)
                if p:
                    p.url_retries += 1
                self._event(ts, level, logger, "photo_retry_url",
                            f"No download URL for photo {pid} "
                            f"(Facebook soft block?), retrying in {m.group(2)} s", pid)
                return True
            m = RE_NO_URL_SKIP.match(msg)
            if m:
                pid = (RE_FBID.search(m.group(1)) or [None, None])[1]
                p = self._photo(job, pid, ts)
                if p:
                    p.status = "missing"
                self._event(ts, level, logger, "photo_missing",
                            f"Photo {pid} SKIPPED: no download URL after all retries. "
                            f"It is missing from this album.", pid)
                return True
            if msg.startswith("Can't find next image in the set"):
                self._set_end("complete", ts)
                return True
            if msg.startswith("Detected a loop in the set"):
                self._set_end("complete_loop", ts)
                return True
            m = RE_JUMP.match(msg)
            if m:
                self._set_end("jump", ts)
                self._event(ts, "warning", logger, "jump",
                            "Album stopped by gallery-dl's 'jump to the beginning' "
                            "heuristic: check the photo count against Facebook", None)
                return True
            if msg.startswith("You've been temporarily blocked"):
                self._set_end("blocked", ts)
                self._event(ts, level, logger, "blocked", msg)
                self._last_record = "event"
                return True
            if msg.startswith("You must be logged in") or msg.startswith("AuthRequired"):
                self._set_end("login", ts)
                self._event(ts, level, logger, "login", msg)
                self._last_record = "event"
                return True
            if msg.startswith("No results for"):
                self._set_end("no_results", ts)
                self._event(ts, "warning", logger, "no_results", msg)
                return True
            m = RE_HTTP_RETRY.match(msg)
            if m:
                self._event(ts, "warning", logger, "request_retry", msg)
                return True
            m = RE_HTTP_FAIL.match(msg)
            if m:
                self._event(ts, level, logger, "http_error", msg)
                return True
            if msg.startswith("An unexpected error occurred"):
                self._set_end("error", ts)
                self._event(ts, level, logger, "unexpected", msg)
                self._last_record = "event"
                return True
            if level == "warning" and ("challenge" in msg.lower() or "captcha" in msg.lower()):
                self._event(ts, level, logger, "challenge", msg)
                return True
            if level == "error":
                self._set_end("error", ts)
                self._event(ts, level, logger, "error", msg)
                self._last_record = "event"
                return True
            if level == "debug" and any(r.match(msg) for r in IGNORED_DEBUG):
                return True
            return False

        # ---- downloader -----------------------------------------------------
        if logger.startswith("downloader"):
            m = RE_ADJUST.match(msg)
            if m:
                if job is not None and job.current is not None:
                    base = os.path.splitext(m.group(1))[0] + "." + m.group(2)
                    job.current.filename = base
                return True
            if level == "warning":
                p = job.current if job is not None else None
                if p is not None:
                    p.download_retries += 1
                kind = "download_retry" if RE_GENERIC_RETRY.match(msg) else "download_warning"
                self._event(ts, level, logger, kind, msg, p.id if p else None)
                self._last_record = "event"
                return True
            if level == "debug" and any(r.match(msg) for r in IGNORED_DEBUG):
                return True
            if level == "error":
                self._event(ts, level, logger, "download_error", msg)
                self._last_record = "event"
                return True
            return False

        # ---- job-level ("download" logger) ---------------------------------------
        if logger == "download":
            m = RE_FAILED_DL.match(msg)
            if m:
                p = job.current if job is not None else None
                if p is not None:
                    p.status = "failed"
                    if p.filename is None:
                        p.filename = os.path.basename(m.group(1))
                self._event(ts, level, logger, "download_failed", msg, p.id if p else None)
                return True
            if msg.startswith("Trying fallback URL"):
                self._event(ts, "warning", logger, "fallback", msg)
                return True
            if level in ("warning", "error"):
                self._event(ts, level, logger, "other_" + level, msg)
                self._last_record = "event"
                return True
            return False

        # ---- input file (-I) ------------------------------------------------
        if logger == "inputfile":
            if level in ("warning", "error"):
                self._event(ts, level, logger, "input_update_failed", msg)
                return True
            return False

        if level == "debug" and any(r.match(msg) for r in IGNORED_DEBUG):
            return True
        return False

    def _photo(self, job: Optional[Job], pid: Optional[str], ts) -> Optional[Photo]:
        if job is None or pid is None:
            return None
        p = job.photos.get(pid)
        if p is None:
            p = job.photos[pid] = Photo(pid, ts, ts)
        return p

    def _on_response(self, ts, host, path, status, length):
        job = self.job
        if host.endswith("fbcdn.net"):
            self.activity = Activity("download", ts)
            if job is None:
                return
            p = job.current
            n = int(length) if length.isdigit() else 0
            if status in (200, 206):
                if p is not None:
                    p.cdn_ok = True
                    p.bytes += n
                job.bytes += n
                self.total_bytes += n
            elif status >= 400:
                self._event(ts, "warning", "urllib3", "cdn_status",
                            f"Image server answered {status}", p.id if p else None)
            return
        if not host.endswith("facebook.com"):
            self.activity = Activity("request", ts)
            return
        self.activity = Activity("request", ts)
        if job is None:
            return
        if path.startswith("/photo"):
            m = RE_FBID.search(path)
            if not m:
                return
            pid = m.group(1)
            cur = job.current
            if cur is not None and cur.id != pid:
                self._finish_photo(job)
            p = self._photo(job, pid, ts)
            p.page_requests += 1
            p.last_ts = ts
            if p.status in ("skipped", "unknown"):
                p.status = "pending"   # retried
            job.current = p
            if status >= 400:
                self._event(ts, "warning", "urllib3", "page_status",
                            f"Photo page answered {status}", pid)
        elif path.startswith("/login"):
            self._event(ts, "warning", "urllib3", "login_redirect",
                        "Redirected to the Facebook login page")
        elif status >= 400 and status != 404:
            self._event(ts, "warning", "urllib3", "page_status",
                        f"Facebook answered {status} for {sanitize_url('https://www.facebook.com' + path)}")

    def finalize_open(self):
        """Nothing to do: open session/job stay 'running' until new lines arrive."""


# --------------------------------------------------------------------------
# Incremental file reader
# --------------------------------------------------------------------------
class LogTail:
    """Reads only appended bytes; detects truncation/rotation and reparses."""

    def __init__(self, path: str):
        self.path = path
        self.state = LogState()
        self.offset = 0
        self.inode = None
        self._rest = b""
        self.resets = 0
        self.size = 0
        self.mtime: Optional[datetime] = None

    def poll(self, max_bytes: int = 64 * 1024 * 1024) -> int:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return 0
        self.size = st.st_size
        self.mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.state = LogState()
            self.offset = 0
            self._rest = b""
            self.resets += 1
        self.inode = st.st_ino
        if st.st_size == self.offset:
            return 0
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            data = fh.read(max_bytes)
        self.offset += len(data)
        data = self._rest + data
        lines = data.split(b"\n")
        self._rest = lines.pop()
        for raw in lines:
            self.state.feed_line(raw.decode("utf-8", errors="replace"))
        return len(lines)
