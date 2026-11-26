import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app.fbdata import FbStore  # noqa: E402
from app.fbpages import parse_albums_page, parse_count  # noqa: E402

ALBUMS = [  # set_id, title, count text  (as the rendered grid shows them)
    ("a.1001", "Vacances 2019", "276 éléments"),
    ("a.1002", "Новый год", "1 élément"),
    ("a.1003", "Été 🌞 Mariage", "1 234 éléments"),       # thin-space thousands
    ("a.1004", "Profile pictures", "12 éléments"),
    ("a.1005", "ألبوم العائلة", "3 éléments"),
]


def fake_page(json_first=2, count_inside=False):
    edges = [{"node": {"__typename": "TimelineAppCollectionItem", "title": {"text": t},
                       "subtitle_text": {"text": c}, "url": f"https://www.facebook.com/media/set/?set={s}&type=3",
                       "node": {"__typename": "Album", "url": f"https://www.facebook.com/media/set/?set={s}&type=3"}},
              "cursor": "x"} for s, t, c in ALBUMS[:json_first]]
    data = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": [[None, None, None, {
        "__bbox": {"result": {"data": {"node": {"all_collections": {"nodes": [{"style_renderer": {
            "collection": {"pageItems": {"edges": edges, "page_info": {"has_next_page": True}}}}}]}}}}}}]]}}]]]}
    tiles = []
    for s, t, c in ALBUMS:
        href = f"https://www.facebook.com/media/set/?set={s}&amp;type=3"
        if count_inside:
            tiles.append(f'<div><a href="{href}"><img src="x.jpg"><div><span>{t}</span></div><span>{c}</span></a></div>')
        else:
            tiles.append(f'<div><a href="{href}"><img src="x.jpg"></a><div><a href="{href}"><span>{t}</span></a>'
                         f'<div><span>{c}</span></div></div></div>')
    return (f'<html><head><title>Some Person | Facebook</title>'
            f'<script type="application/json">{json.dumps(data)}</script></head><body>'
            f'<a href="/some.user/friends/">Amis</a><span>3 h</span>'
            f'<a href="https://www.facebook.com/photo/?fbid=99&amp;set=a.1001">photo</a>'
            + "".join(tiles) + "<span>12 j</span></body></html>")


@pytest.mark.parametrize("inside", [False, True])
def test_parse_grid_and_json(inside):
    page = parse_albums_page(fake_page(count_inside=inside))
    assert page.page_title == "Some Person | Facebook"
    got = {s: (a.title, a.count) for s, a in page.albums.items()}
    assert got == {"a.1001": ("Vacances 2019", 276), "a.1002": ("Новый год", 1),
                   "a.1003": ("Été 🌞 Mariage", 1234), "a.1004": ("Profile pictures", 12),
                   "a.1005": ("ألبوم العائلة", 3)}
    assert list(page.albums) == [s for s, _, _ in ALBUMS]            # page order
    assert page.json_albums == 2 and page.albums["a.1001"].source == "grid+json"
    assert not page.warnings


def test_html_only_page_is_explained():
    page = parse_albums_page("<html><head><title>x</title><script>{}</script></head><body></body></html>")
    assert not page.albums and "Web Page, Complete" in page.warnings[0]


def test_counts():
    assert parse_count("276 éléments") == (276, False)
    assert parse_count("1 élément") == (1, False)
    assert parse_count("1 234 éléments") == (1234, False)
    assert parse_count("12 items") == (12, False)
    assert parse_count("1,2 k éléments") == (1200, True)
    assert parse_count("3 h") is None and parse_count("Vacances 2019") is None


def test_store_merge_and_forget(tmp_path):
    st = FbStore(str(tmp_path / "facebook-albums.json"))
    r = st.import_page(parse_albums_page(fake_page()))
    assert r == {"albums": 5, "new": 5, "updated": 0}
    assert st.counts()["a.1003"] == (1234, False) and st.titles()["a.1002"] == "Новый год"
    assert st.import_page(parse_albums_page(fake_page()))["new"] == 0
    st.forget_page("Some Person | Facebook")
    assert st.snapshot() == {}


def test_disk_check_uses_facebook_counts(tmp_path):
    sys.path.insert(0, HERE)
    from test_integrity import Lib
    from app import integrity as ig
    L = Lib(str(tmp_path / "fb"))
    L.chain("A", "Complete", "a.1", ["101", "102", "103"])
    L.chain("A", "Short", "a.2", ["201", "202"])          # chain intact, but Facebook has 3
    L.chain("A", "Extra", "a.3", ["301", "302", "303"])
    L.chain("A", "Rounded", "a.4", ["401", "402"])
    L.chain("A", "Current", "a.5", ["501"])
    fb = {"a.1": (3, False), "a.2": (3, False), "a.3": (2, False), "a.4": (2100, True),
          "a.5": (50, False), "a.9": (10, False)}             # a.9 not downloaded at all
    s = ig.IntegrityScanner(L.root, None, None, max_mbps=1000, current_set=lambda: "a.5",
                            fb_counts=lambda: fb)
    s.scan_once()
    r = s.report.albums
    assert r["a.1"].fb_diff == 0 and r["a.1"].verdict == "verified"
    assert r["a.2"].fb_diff == 1 and r["a.2"].missing_estimate == 1 and r["a.2"].verdict == "problems"
    assert "Facebook shows 3 photos, 2 on disk" in " ".join(i.text for i in r["a.2"].issues)
    assert r["a.3"].fb_diff == -1 and r["a.3"].verdict == "verified"   # info only
    assert r["a.4"].verdict == "problems"                     # far beyond rounding
    assert r["a.5"].fb_diff is None                           # being downloaded
    assert "a.9" not in r                                     # listed on the Facebook tab instead


def test_import_route_keeps_no_html(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    for k, v in {"DATA_DIR": str(tmp_path), "INTEGRITY": "0", "LOG_FILE": str(tmp_path / "x.log"),
                 "ALBUMS_FILE": str(tmp_path / "x.txt"), "DOWNLOADS_ROOT": str(tmp_path / "dl")}.items():
        monkeypatch.setenv(k, v)
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    page = fake_page().replace("</body>", '<script>{"fb_dtsg":"SECRET-TOKEN"}</script></body>')
    with TestClient(m.app) as c:
        files = [("files", ("albums.html", page.encode(), "text/html"))]
        assert c.post("/facebook/import", files=files,
                      headers={"sec-fetch-site": "cross-site"}).status_code == 403
        r = c.post("/facebook/import", files=files, headers={"sec-fetch-site": "same-origin"},
                   follow_redirects=False)
        assert r.status_code == 303 and "5 album" in r.headers["location"].replace("%20", " ")
        html = c.get("/facebook").text
        assert "Nothing from this page is downloaded or in albums.txt" in html
        # albums listed in the page's order, numbered #1..#5
        import re as _re
        order = _re.findall(r'<tr id="fb-(a\.\d+)"', html)
        assert order == ["a.1001", "a.1002", "a.1003", "a.1004", "a.1005"]
    stored = [p for p in os.listdir(tmp_path) if not p.startswith(".")]
    assert "facebook-albums.json" in stored
    for name in stored:                                   # nothing but extracted data
        p = os.path.join(tmp_path, name)
        if os.path.isfile(p):
            body = open(p, encoding="utf-8", errors="replace").read()
            assert "SECRET-TOKEN" not in body and "<html" not in body.lower()


def test_facebook_order_summary(tmp_path, monkeypatch):
    """Which part of the page was downloaded: first/last position and gaps."""
    pytest.importorskip("httpx")
    sys.path.insert(0, HERE)
    from test_integrity import Lib
    root = tmp_path / "dl"
    L = Lib(str(root))
    for sid in ("a.1002", "a.1004"):                      # #2 and #4 downloaded, #3 skipped
        L.chain("Acc", "x", sid, [sid[2:] + "1"])
    for k, v in {"DATA_DIR": str(tmp_path), "LOG_FILE": str(tmp_path / "x.log"),
                 "ALBUMS_FILE": str(tmp_path / "x.txt"), "DOWNLOADS_ROOT": str(root),
                 "INTEGRITY": "1"}.items():
        monkeypatch.setenv(k, v)
    import importlib
    import app.main as m
    m = importlib.reload(m)
    m.FBSTORE.import_page(parse_albums_page(fake_page()))
    m.SCANNER.scan_once()
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        html = c.get("/facebook").text
    m.SCANNER.stop.set()
    m.SCANNER.poke.set()
    assert "from <b>#2</b> to <b>#4</b>" in html
    assert 'skipped in between: <a href="#fb-a.1003">#3</a>' in html
    assert "#1–#1 not started" in html and "#5–#5 not started" in html


def test_old_import_without_positions_asks_for_reimport(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    for k, v in {"DATA_DIR": str(tmp_path), "INTEGRITY": "0", "LOG_FILE": str(tmp_path / "x.log"),
                 "ALBUMS_FILE": str(tmp_path / "x.txt"), "DOWNLOADS_ROOT": str(tmp_path / "dl")}.items():
        monkeypatch.setenv(k, v)
    # data saved by the previous version: no "pos"
    old = {"albums": {s: {"title": t, "count": 1, "approximate": False, "page": "P", "imported": "x"}
                      for s, t, _ in ALBUMS}}
    (tmp_path / "facebook-albums.json").write_text(json.dumps(old), encoding="utf-8")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        assert "import it again" in c.get("/facebook").text
        c.post("/facebook/import", files=[("files", ("p.html", fake_page().encode(), "text/html"))],
               headers={"sec-fetch-site": "same-origin"})
        html = c.get("/facebook").text
    # the re-import replaces the page's albums, now with positions (page title differs here,
    # so the old "P" page still asks; the new one has its order)
    assert '<td class="num">1</td>' in html and '<td class="num">5</td>' in html
