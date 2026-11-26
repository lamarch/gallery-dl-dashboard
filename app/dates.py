"""Read-only check of the dates written by fix_order.py (mtime + EXIF/XMP/QuickTime).

For every album it answers:
  * has fix_order been run? (all / some / none of the files carry its dates)
  * what would fix_order change if it ran now with these settings (tz, step)?
    and above all: would it REWRITE files it had already corrected?
    (that happens when the album changed since, e.g. a missing photo was
    recovered or the download resumed, or with another --tz / --step;
    rewriting a file already imported into Immich can make Immich treat it
    as a new asset)
  * do target timestamps collide inside the album or with other albums?

The ordering and date logic is a line-by-line port of fix_order.py
(chain(), ordre_num(), plan_mtimes(), valeurs_attendues()); tests compare it
with the real script. Nothing here writes to any file.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# fix_order.py constants
IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".m4v", ".mov"}
EXTENSIONS = IMAGES | VIDEOS | {".gif"}
TOLERANCE_MTIME = 0.001


# ----------------------------------------------------------------------------- port
def parse_date(valeur):
    if isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        try:
            return datetime.fromtimestamp(valeur, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(valeur, str) or not valeur:
        return None
    for f in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
              "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
              "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(valeur, f)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def chain(photos, notes):
    suivants, absents = {}, []
    for pid, p in photos.items():
        nxt = p["next"]
        if nxt in (None, ""):
            continue
        nxt = str(nxt)
        if nxt == pid:
            continue
        if nxt not in photos:
            absents.append((pid, nxt))
            continue
        suivants[pid] = nxt
    if len(absents) > 1:
        notes.append(("warn", f"{len(absents)} photos point to a next photo that is not on disk"))
    pointes = set(suivants.values())
    tetes = sorted(pid for pid in photos if pid not in pointes)
    composantes, vus = [], set()
    for tete in tetes:
        seq, cur = [], tete
        while cur is not None:
            if cur in vus:
                notes.append(("warn", f"chain from {tete} joins an already placed photo"))
                break
            seq.append(cur)
            vus.add(cur)
            cur = suivants.get(cur)
        composantes.append(seq)
    reste = sorted(pid for pid in photos if pid not in vus)
    if reste:
        notes.append(("warn", f"{len(reste)} photo(s) in a cycle: order estimated by ID"))
        composantes.append(reste)
    if len(composantes) > 1:
        notes.append(("warn", f"album in {len(composantes)} segments: order between them is estimated"))
    return composantes


def ordre_num(photos, notes):
    par_num = defaultdict(list)
    for pid, p in photos.items():
        par_num[p["num"]].append(pid)
    if any(len(ids) > 1 and n for n, ids in par_num.items()):
        notes.append(("warn", "duplicate 'num' values: order uncertain"))
    return [sorted(photos, key=lambda pid: (photos[pid]["num"] or 0, str(photos[pid]["id"])))]


def plan_targets(composantes, photos, pas):
    """-> [(pid, target_utc, rank, group_size)], overflows [(base, until, next_base)]"""
    ordre = [pid for comp in composantes for pid in comp]
    par_date = defaultdict(list)
    for pid in ordre:
        par_date[photos[pid]["date"].replace(microsecond=0)].append(pid)
    bases = sorted(par_date)
    out, overflows = [], []
    for i, d in enumerate(bases):
        groupe = par_date[d]
        n = len(groupe)
        decalage_max = (n - 1) * pas
        if n > 1 and i + 1 < len(bases) and d + timedelta(seconds=decalage_max) >= bases[i + 1]:
            overflows.append((d, d + timedelta(seconds=decalage_max), bases[i + 1]))
        for rang, pid in enumerate(groupe):
            out.append((pid, d + timedelta(seconds=rang * pas), rang, n))
    return out, overflows


def expected_exif(cible_utc, tz):
    """fix_order.valeurs_attendues(): (local date, ms or '-', offset)."""
    local = cible_utc.astimezone(tz)
    off = local.strftime("%z")
    ms = f"{local.microsecond // 1000:03d}" if local.microsecond else "-"
    return (local.strftime("%Y:%m:%d %H:%M:%S"), ms, f"{off[:3]}:{off[3:]}")


# ----------------------------------------------------------------------------- reading
def embedded_instant(r) -> Optional[datetime]:
    """Instant stored in the file by fix_order (UTC), or None."""
    if r is None or not r.dt_value:
        return None
    try:
        if r.dt_kind == "qt":
            return datetime.strptime(r.dt_value, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if r.dt_kind == "xmp":
            v = r.dt_value.replace(":", "-", 2) if r.dt_value[4:5] == ":" else r.dt_value
            return datetime.fromisoformat(v.replace(" ", "T")).astimezone(timezone.utc)
        if r.dt_kind == "exif":
            d = datetime.strptime(r.dt_value[:19], "%Y:%m:%d %H:%M:%S")
            if r.dt_subsec and r.dt_subsec.isdigit():
                d = d.replace(microsecond=int((r.dt_subsec + "000")[:3]) * 1000)
            if not r.dt_offset:
                return None                      # local time without zone: not comparable
            sign = -1 if r.dt_offset.startswith("-") else 1
            hh, mm = r.dt_offset[1:].split(":")
            off = timedelta(hours=int(hh), minutes=int(mm)) * sign
            return d.replace(tzinfo=timezone(off)).astimezone(timezone.utc)
    except (ValueError, IndexError):
        return None
    return None


# ----------------------------------------------------------------------------- report
@dataclass
class FileDate:
    entry: object            # integrity.FileEntry
    target: datetime         # what fix_order would write now
    rank: int                # position inside its same-second batch
    group: int               # size of that batch
    mtime_ok: bool           # mtime == target
    embedded: Optional[datetime]   # date tag in the file (UTC), None if absent
    tag_ok: bool             # date tag is exactly what fix_order would write now
    would_mtime: bool        # fix_order would change the mtime
    would_content: bool      # fix_order would change the file's bytes
    corrected: bool          # fix_order already wrote a date tag in this file

    @property
    def mtime_dt(self) -> datetime:
        r = self.entry.result
        ns = r.mtime_ns if r else self.entry._st.st_mtime_ns
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)

    @property
    def timeline(self) -> datetime:
        """Date Immich shows in its timeline (metadata.service getDates()):
        the embedded date when present, otherwise the file time (mtime)."""
        return self.embedded or self.mtime_dt

    @property
    def rewrite_corrected(self) -> bool:
        """fix_order would modify a file it had already corrected."""
        return self.corrected and (self.would_mtime or self.would_content)

    @property
    def state(self) -> str:
        if self.mtime_ok and self.tag_ok:
            return "ok"
        if not self.corrected and not self.mtime_ok:
            return "never"
        if self.rewrite_corrected:
            return "changed"
        return "partial"


@dataclass
class AlbumDates:
    set_id: str
    files: list = field(default_factory=list)       # FileDate
    skipped: list = field(default_factory=list)     # (entry, reason)
    notes: list = field(default_factory=list)       # (severity, text)
    overflows: list = field(default_factory=list)
    inner_collisions: int = 0
    cross_collisions: dict = field(default_factory=dict)   # other set_id -> count
    offsets: Counter = field(default_factory=Counter)
    ready: bool = False
    ready_reason: str = ""
    interleaved_by: list = field(default_factory=list)   # Interleave where this album is "a"

    def count(self, attr) -> int:
        return sum(1 for f in self.files if getattr(f, attr))

    @property
    def state(self) -> str:
        if not self.files:
            return "nothing"
        states = Counter(f.state for f in self.files)
        if states["changed"]:
            return "changed"
        if states["ok"] == len(self.files):
            return "applied"
        if states["never"] == len(self.files):
            return "not run"
        return "partial"

    @property
    def danger(self) -> int:
        """Already-corrected files fix_order would modify if run now."""
        return sum(1 for f in self.files if f.rewrite_corrected)

    @property
    def danger_content(self) -> int:
        return sum(1 for f in self.files if f.corrected and f.would_content)


@dataclass
class DatesReport:
    tz_name: str
    step: float
    albums: dict = field(default_factory=dict)       # set_id -> AlbumDates
    offsets: Counter = field(default_factory=Counter)
    steps_seen: Counter = field(default_factory=Counter)
    tz_mismatch: int = 0                             # corrected files written with another tz
    interleaved: list = field(default_factory=list)  # Interleave, worst first

    def counts(self) -> Counter:
        return Counter(a.state for a in self.albums.values())


def build_dates(entries, tz, tz_name: str, step: float, checks: dict,
                current_set: Optional[str]) -> DatesReport:
    """entries: integrity.FileEntry list (with .meta and .result);
    checks: integrity AlbumCheck per set_id (for the 'ready' decision)."""
    rep = DatesReport(tz_name, step)
    sets = defaultdict(list)
    for e in entries:
        if e.ext not in EXTENSIONS or not e.has_json or e.meta is None:
            continue
        if e.meta.problem and e.meta.problem.startswith(("JSON unreadable", "JSON is not")):
            continue
        sets[e.meta.set_id or "?"].append(e)

    all_targets = defaultdict(set)      # target(ms) -> set_ids
    for sid, es in sets.items():
        a = rep.albums[sid] = AlbumDates(sid)
        photos = {}
        for e in sorted(es, key=lambda x: x.path):
            m = e.meta
            pid = m.id or ("fichier:" + os.path.basename(e.path))
            d = parse_date(m.date)
            if d is None:
                a.skipped.append((e, "no usable date in JSON: fix_order ignores it"))
                continue
            if pid in photos:
                photos[pid]["entries"].append(e)
                continue
            photos[pid] = {"id": m.id, "num": m.num, "next": m.next_id, "date": d, "entries": [e]}
        if not photos:
            continue
        if any(p["next"] for p in photos.values()):
            comps = chain(photos, a.notes)
        else:
            if len(photos) > 1:
                a.notes.append(("info", "no next_photo_id: ordered by 'num'"))
            comps = ordre_num(photos, a.notes)
        targets, a.overflows = plan_targets(comps, photos, step)
        if a.overflows:
            a.notes.append(("warn", f"{len(a.overflows)} same-second batch(es) spread past the next "
                                    f"batch: photos of both batches interleave (smaller --step?)"))
        seen = Counter()
        for pid, target, rank, n in targets:
            key = round(target.timestamp() * 1000)
            seen[key] += 1
            all_targets[key].add(sid)
            for e in photos[pid]["entries"]:
                a.files.append(_classify(e, target, rank, n, tz, step, rep))
        a.inner_collisions = sum(c - 1 for c in seen.values() if c > 1)
        if a.inner_collisions:
            a.notes.append(("warn", f"{a.inner_collisions} photo(s) share their exact timestamp with "
                                    f"another photo of the album"))
        for f in a.files:
            if f.embedded is not None and f.entry.result and f.entry.result.dt_offset:
                a.offsets[f.entry.result.dt_offset] += 1
        rep.offsets.update(a.offsets)

    # collisions between albums
    for key, sids in all_targets.items():
        if len(sids) > 1:
            for s1 in sids:
                for s2 in sids:
                    if s1 != s2:
                        rep.albums[s1].cross_collisions[s2] = rep.albums[s1].cross_collisions.get(s2, 0) + 1

    rep.interleaved = find_interleaving(rep.albums)

    # ready to run fix_order? only on albums proven complete
    for sid, a in rep.albums.items():
        c = checks.get(sid)
        if sid == current_set or (c is not None and c.in_progress):
            a.ready, a.ready_reason = False, "being downloaded"
        elif c is None or c.verdict == "scanning":
            a.ready, a.ready_reason = False, "disk check not finished"
        elif c.verdict == "problems":
            a.ready, a.ready_reason = False, "disk check found problems (missing or damaged photos)"
        elif c.log_passes and not c.log_confirmed:
            # the album may still grow: correcting it now risks rewrites later
            a.ready, a.ready_reason = False, "last pass in the log did not reach the end of the album"
        elif not c.log_confirmed:
            a.ready, a.ready_reason = True, "complete on disk (not in the current log)"
        else:
            a.ready, a.ready_reason = True, "verified complete (disk + log)"
        if a.ready and c.verdict == "warnings":
            a.ready_reason += "; disk check has warnings"
    return rep


@dataclass
class Interleave:
    a: str                    # album whose time span is entered
    b: str                    # album with photos inside it
    inside: int               # photos of b strictly inside a's span
    start: datetime           # overlap of the two spans
    end: datetime
    a_fixed: bool             # fix_order applied (its photos sit at their final place)
    b_fixed: bool


def find_interleaving(albums: dict) -> list:
    """Albums whose photos fall inside another album's time span, in the
    timeline as Immich shows it now. Albums are long: two albums of the same
    weekend overlapping is expected; what matters is how many photos of one
    land between the photos of the other."""
    import bisect
    times = {}
    for sid, a in albums.items():
        ts = sorted(f.timeline for f in a.files)
        if ts:
            times[sid] = ts
    out = []
    for sa, ta in times.items():
        a0, a1 = ta[0], ta[-1]
        if a0 == a1:
            continue
        for sb, tb in times.items():
            if sb == sa:
                continue
            lo = bisect.bisect_right(tb, a0)
            hi = bisect.bisect_left(tb, a1)
            inside = hi - lo
            if inside > 0:
                fixed = lambda x: albums[x].state == "applied"  # noqa: E731
                out.append(Interleave(sa, sb, inside, max(a0, tb[0]), min(a1, tb[-1]),
                                      fixed(sa), fixed(sb)))
    out.sort(key=lambda i: (-i.inside, i.a, i.b))
    for i in out:
        albums[i.a].interleaved_by.append(i)
    return out


def _classify(e, target, rank, n, tz, step, rep) -> FileDate:
    r = e.result
    ts = target.timestamp()
    mtime = (r.mtime_ns if r else e._st.st_mtime_ns) / 1e9
    mtime_ok = abs(mtime - ts) < TOLERANCE_MTIME
    emb = embedded_instant(r)
    emb_ok = emb is not None and abs(emb.timestamp() - ts) < 0.0015
    exp = expected_exif(target, tz)
    if e.ext in IMAGES:
        exif = r is not None and r.dt_kind == "exif"
        actual = ((r.dt_value or "-")[:19] if exif else "-",
                  (r.dt_subsec or "-") if exif else "-",
                  (r.dt_offset or "-") if exif else "-")
        tag_ok = actual == exp
        if emb_ok and not tag_ok:
            rep.tz_mismatch += 1          # right instant, written with another --tz
    elif e.ext in VIDEOS:
        tag_ok = emb_ok                   # QuickTime dates are UTC: tz-independent
    else:                                 # GIF: XMP DateCreated with the local offset
        want = exp[0].replace(":", "-", 2).replace(" ", "T") + exp[2]
        tag_ok = bool(r and r.dt_kind == "xmp" and r.dt_value == want)
    # fix_order rewrites videos/GIFs on every run, but exiftool then produces
    # identical bytes when the values are unchanged (verified with exiftool 12.76)
    would_content = not tag_ok
    # which --step was used? (from batches with a date tag)
    if emb is not None and rank > 0:
        base = target - timedelta(seconds=rank * step)
        rep.steps_seen[round((emb - base).total_seconds() / rank, 3)] += 1
    # "already corrected by fix_order?" Facebook strips photo metadata, so any
    # EXIF date in a photo was written by fix_order. Videos can carry an encoder
    # creation time: count them only when the tag matches the target or the mtime
    # (fix_order writes both with the same value).
    if e.ext in IMAGES:
        corrected = emb is not None
    else:
        corrected = emb is not None and (emb_ok or abs(emb.timestamp() - mtime) < 1.5)
    return FileDate(e, target, rank, n, mtime_ok, emb, tag_ok,
                    not mtime_ok, would_content, corrected)
