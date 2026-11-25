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
