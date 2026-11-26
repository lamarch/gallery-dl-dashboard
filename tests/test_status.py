import os
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from app.albums import read_albums  # noqa: E402
from app.logparse import LogState  # noqa: E402
from app.status import album_rows, header  # noqa: E402
from fakelog import FakeLog  # noqa: E402


def st_of(f):
    st = LogState()
    for line in f.text().splitlines():
        st.feed_line(line)
    return st


def base():
    f = FakeLog().session_start()
    f.job_start("a.1")
    f.photo("11").photo("12")
    return f


def test_working_then_stalled():
    st = st_of(base())
    h = header(st, [], None, now=st.last_ts + timedelta(seconds=20))
    assert h.state == "downloading" and h.album_downloaded == 2
    h = header(st, [], None, now=st.last_ts + timedelta(minutes=6))
    assert h.state == "stalled"


def test_pause_and_ratelimit():
    f = FakeLog().session_start()
    f.log("gallery-dl", "debug", "Starting DownloadJob for 'https://www.facebook.com/media/set/?set=a.9'")
    f.log("facebook", "debug", "Sleeping 3600.00 seconds (extractor)")
    st = st_of(f)
    h = header(st, [], None, now=st.last_ts + timedelta(minutes=30))
    assert h.state == "pause" and h.until == st.last_ts + timedelta(seconds=3600)
    h = header(st, [], None, now=st.last_ts + timedelta(minutes=70))
    assert h.state == "stalled"
    f = base().page("13").no_url_retry("13")
    lines = f.text().splitlines()
    st = LogState()
    for line in lines[:-0 or None]:
        st.feed_line(line)
    h = header(st, [], None, now=st.last_ts - timedelta(seconds=600))
    assert h.state == "ratelimit"


def test_finished_and_blocked():
    st = st_of(base().end_chain().wrapper_exit(0))
    assert header(st, [], None).state == "finished"
    st = st_of(base().page("13").blocked("13"))
    assert header(st, [], None, now=st.last_ts).state == "blocked"


def test_albums_txt_and_rows(tmp_path):
    p = tmp_path / "albums.txt"
    p.write_text("# my comment\n# https://www.facebook.com/media/set/?set=a.1&type=3\n"
                 "https://www.facebook.com/media/set/?set=a.2&type=3  # note\n-option=1\n\n",
                 encoding="utf-8")
    albums, err = read_albums(str(p))
    assert err is None
    assert [(a.set_id, a.done) for a in albums] == [("a.1", True), ("a.2", False)]
    st = st_of(base().page("13").no_url_skip("13").end_chain())
    rows = album_rows(st, albums, None, None)
    r1 = rows[0]
    assert r1.state == "done" and r1.missing == ["13"] and r1.severity == "bad"
    assert rows[1].state == "pending" and rows[1].passes == 0


def test_queue_eta_uses_facebook_sizes():
    from app.albums import InputAlbum
    from app.status import EXTRACTOR_PAUSE, plan_queue
    f = FakeLog().session_start()
    f.job_start("a.1", sleep_extractor=0)
    for i in range(10):
        f.photo(str(100 + i))                                # 10 photos seen, ~23.5 s each
    st = st_of(f)
    albums = [InputAlbum(1, 1, "a.1", "u", False), InputAlbum(2, 2, "a.2", "u", False),
              InputAlbum(3, 3, "a.3", "u", True), InputAlbum(4, 4, "a.4", "u", False)]
    now = st.last_ts
    etas, end, spp, avg = plan_queue(st, albums, None, {"a.1": (30, False), "a.2": (100, False)}, now)
    assert set(etas) == {"a.1", "a.2", "a.4"}                # a.3 is done
    cur = etas["a.1"]
    assert abs((cur.finish - now).total_seconds() - 20 * spp) < 1e-6     # 30 expected - 10 seen
    a2 = etas["a.2"]
    assert a2.start == cur.finish and a2.from_fb
    assert abs((a2.finish - a2.start).total_seconds() - (EXTRACTOR_PAUSE + 100 * spp)) < 1e-6
    assert etas["a.4"].start == a2.finish and not etas["a.4"].from_fb and etas["a.4"].size == avg
    h = header(st, albums, None, now=now, fb={"a.1": (30, False), "a.2": (100, False)})
    assert h.album_expected == 30 and h.album_eta_at == cur.finish
    assert abs(h.eta_seconds - (end - now).total_seconds()) < 1e-6


def test_expected_column_coherence(tmp_path):
    import sys as _s
    _s.path.insert(0, HERE)
    from test_integrity import Lib
    from app import disk as disk_mod
    from app.albums import InputAlbum
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "One", "a.1", ["11", "12"])
    L.chain("A", "Two", "a.2", ["21"])
    f = FakeLog().session_start()
    f.job_start("a.1", sleep_extractor=0).photo("11").photo("12").end_chain()
    f.job_start("a.2", sleep_extractor=0).photo("21").end_chain()
    f.job_start("a.3", sleep_extractor=0)
    st = st_of(f)
    disk = disk_mod.scan(L.root)
    albums = [InputAlbum(1, 1, "a.1", "u", True), InputAlbum(2, 2, "a.2", "u", True)]
    rows = {r.set_id: r for r in album_rows(st, albums, disk, "a.3",
                                             fb={"a.1": (2, False), "a.2": (3, False)},
                                             fb_titles={"a.2": "Titre Facebook"})}
    assert rows["a.1"].expected_ok is True and rows["a.1"].log_full == 2
    r2 = rows["a.2"]
    assert r2.expected_ok is False and r2.title == "Titre Facebook"
    texts = " ".join(t for _, t in r2.flags)
    assert "Facebook shows 3 photo(s), 1 on disk" in texts
    assert "last complete pass saw 1 photo(s), Facebook shows 3" in texts


def test_pending_album_is_not_flagged_against_facebook():
    from app.albums import InputAlbum
    st = st_of(base())                                       # a.1 being downloaded
    rows = album_rows(st, [InputAlbum(1, 1, "a.1", "u", False), InputAlbum(2, 2, "a.9", "u", False)],
                      None, "a.1", fb={"a.9": (30, False), "a.1": (5, False)}, fb_titles={"a.9": "Plus tard"})
    r9 = next(r for r in rows if r.set_id == "a.9")
    assert r9.state == "pending" and r9.fb_count == 30 and r9.flags == [] and r9.expected_ok is None
    assert r9.title == "Plus tard" and r9.eta is not None


def test_expected_totals_and_size(tmp_path):
    import sys as _s
    _s.path.insert(0, HERE)
    from test_integrity import Lib
    from app import disk as disk_mod
    from app.albums import InputAlbum
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "One", "a.1", ["11", "12", "13", "14"])     # done, 4 on disk
    L.chain("A", "Two", "a.2", ["21"])                        # current, 1 on disk
    disk = disk_mod.scan(L.root)
    albums = [InputAlbum(1, 1, "a.1", "u", True), InputAlbum(2, 2, "a.2", "u", False),
              InputAlbum(3, 3, "a.3", "u", False)]           # a.3: no Facebook count
    st = st_of(base())
    h = header(st, albums, disk, now=st.last_ts, fb={"a.1": (4, False), "a.2": (10, False)})
    assert h.albums_count_unknown == 1
    assert h.photos_expected == 4 + 10 + 4                    # a.3 at the average album size (4)
    assert h.photos_have == 5
    bpp = disk.total_bytes / disk.total_media
    assert abs(h.est_total_bytes - 18 * bpp) < 1e-6 and abs(h.est_remaining_bytes - 13 * bpp) < 1e-6
    disk.free_bytes = int(10 * bpp)                           # not enough room for 13 photos
    assert header(st, albums, disk, now=st.last_ts, fb={"a.1": (4, False), "a.2": (10, False)}).space_warning


def test_totals_count_each_album_once(tmp_path):
    """albums.txt lines for the same album (a "...&setextract" re-download line,
    a duplicate) count once; extra photos in one album never hide missing ones
    in another; lines that are not albums are left out."""
    import sys as _s
    _s.path.insert(0, HERE)
    from test_integrity import Lib
    from app import disk as disk_mod
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "One", "a.1", ["11", "12", "13", "14", "15"])     # 5 on disk, Facebook says 3
    L.chain("A", "Two", "a.2", ["21", "22"])                       # 2 on disk, Facebook says 4
    disk = disk_mod.scan(L.root)
    p = tmp_path / "albums.txt"
    p.write_text("# https://www.facebook.com/media/set/?set=a.1&type=3\n"
                 "# https://www.facebook.com/media/set/?set=a.2&type=3\n"
                 "https://www.facebook.com/photo/?fbid=23&set=a.2&setextract\n"   # re-download line
                 "# https://www.facebook.com/media/set/?set=a.1&type=3\n"          # duplicate
                 "https://www.facebook.com/some.profile/photos_albums\n", encoding="utf-8")
    albums, _ = read_albums(str(p))
    fb = {"a.1": (3, False), "a.2": (4, False)}
    st = st_of(base())
    h = header(st, albums, disk, now=st.last_ts, fb=fb)
    assert h.total == 2 and h.done == 1                  # a.2 is being re-walked
    assert h.photos_expected == 7 and h.photos_have == 7
    assert h.photos_remaining == 2                       # a.2 misses 2; a.1's extras don't hide them
    assert h.lines_not_albums == 1
    rows = album_rows(st, albums, disk, None, fb=fb)
    assert [r.set_id for r in rows if r.pos] == ["a.1", "a.2"]     # one row per album
