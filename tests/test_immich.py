import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app import immich  # noqa: E402

ROOT = "/ext/facebook/"
KEY = "k" * 20


def asset(i, folder, name, offline=False):
    return {"id": f"x{i}", "originalPath": f"{ROOT}{folder}/{name}", "isOffline": offline, "isTrashed": False}


ASSETS = [asset(1, "Acc/Trip (a.1)", "20190101_000000_11.jpg"),
          asset(2, "Acc/Trip (a.1)", "20190101_000001_12.jpg"),
          asset(3, "Acc/Party (a.2)", "20200101_000000_21.jpg"),          # a.2: 1 of 2 in Immich
          asset(4, "Acc/Old (a.3)", "20200101_000000_31.jpg"),
          asset(5, "Acc/Dup (a.4)", "20200101_000000_41.jpg", offline=True),
          {"id": "y", "originalPath": "/photos/phone/IMG_1.jpg", "isOffline": False, "isTrashed": False}]
ALBUMS = [
    {"id": "u1", "albumName": "Summer trip", "assetCount": 2, "order": "asc",
     "description": "Facebook album: https://www.facebook.com/media/set/?set=a.1", "albumThumbnailAssetId": "x1"},
    {"id": "u3", "albumName": "Acc Old (a.3)", "assetCount": 1, "order": "desc", "description": ""},
    {"id": "u4a", "albumName": "Dup", "assetCount": 1, "order": "asc",
     "description": "Facebook album: https://www.facebook.com/media/set/?set=a.4"},
    {"id": "u4b", "albumName": "Dup (a.4)", "assetCount": 1, "order": "desc"},
    {"id": "u9", "albumName": "My best of", "assetCount": 3, "order": "desc"},
]


class Fake(BaseHTTPRequestHandler):
    legacy = False
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Fake.seen.append(("GET", self.path))
        if self.headers.get("x-api-key") != KEY:
            return self._send(401, {})
        if self.path == "/api/albums":
            return self._send(200, ALBUMS)
        if self.path.startswith("/api/assets/"):
            a = next((a for a in ASSETS if a["id"] == self.path.rsplit("/", 1)[1]), None)
            return self._send(200 if a else 404, a or {})
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append(("POST", self.path, body))
        if self.path != "/api/search/metadata":
            return self._send(405, {})         # anything that would write: never called
        if "filter" in body:
            if Fake.legacy:
                return self._send(400, {})
            pre = (body["filter"].get("originalPath") or {}).get("startsWith", "")
            items = [a for a in ASSETS if a["originalPath"].startswith(pre)]
            start = int(body.get("cursor") or 0)
            page = items[start:start + 2]       # tiny pages: exercise the cursor
            nxt = str(start + 2) if start + 2 < len(items) else None
            return self._send(200, {"assets": {"items": page, "nextCursor": nxt}})
        if body.get("visibility") == "archive":
            return self._send(200, {"assets": {"items": [], "nextPage": None}})
        p = body["page"]
        page = ASSETS[(p - 1) * 4:p * 4]
        return self._send(200, {"assets": {"items": page, "nextPage": str(p + 1) if p * 4 < len(ASSETS) else None}})


@pytest.fixture
def server():
    Fake.seen, Fake.legacy = [], False
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture
def keyfile(tmp_path):
    p = tmp_path / "immich.key"
    p.write_text(KEY + "\n")
    os.chmod(p, 0o600)
    return str(p)


def test_read_key_refuses_readable_files(tmp_path, keyfile):
    assert immich.read_key(keyfile) == KEY
    os.chmod(keyfile, 0o644)
    with pytest.raises(immich.ImmichError, match="chmod 600"):
        immich.read_key(keyfile)
    with pytest.raises(immich.ImmichError):
        immich.read_key(str(tmp_path / "missing"))


@pytest.mark.parametrize("legacy", [False, True])
def test_refresh_reads_only(server, keyfile, legacy):
    Fake.legacy = legacy
    w = immich.ImmichWatcher(server, keyfile)
    w.refresh()
    assert w.error is None
    s = w.snap
    assert s.prefix == ROOT
    assert sorted(a["id"] for a in s.assets) == ["x1", "x2", "x3", "x4", "x5"]   # phone photo dropped
    assert s.method == ("paged" if legacy else "filter")
    assert all(c[0] == "GET" or c[1] == "/api/search/metadata" for c in Fake.seen)
    if not legacy:   # limited to the downloads' path
        assert all(c[2]["filter"]["originalPath"]["startsWith"] == ROOT for c in Fake.seen if c[0] == "POST")
    # the API key never ends up in an error message
    w2 = immich.ImmichWatcher(server, keyfile)
    w2.key_file = keyfile
    os.chmod(keyfile, 0o600)
    with open(keyfile, "w") as fh:
        fh.write("wrong-key")
    w2.refresh()
    assert "401" in w2.error and "wrong-key" not in w2.error


def test_unreachable_immich(keyfile):
    w = immich.ImmichWatcher("http://127.0.0.1:9", keyfile)
    w.refresh()
    assert w.error.startswith("cannot reach Immich") and w.snap is None


def _disk(folder, ids):
    return SimpleNamespace(folder=folder, media=len(ids), media_ids=set(ids))


def test_report(server, keyfile):
    w = immich.ImmichWatcher(server, keyfile)
    w.refresh()
    disk = {"a.1": _disk("Trip (a.1)", ["11", "12"]),
            "a.2": _disk("Party (a.2)", ["21", "22"]),
            "a.3": _disk("Old (a.3)", ["31"]),
            "a.4": _disk("Dup (a.4)", ["41"]),
            "a.5": _disk("New (a.5)", ["51"]),
            "?Acc/stray": _disk("stray", ["1"])}
    chosen = {"a.1": ("Summer trip", "facebook"), "a.2": ("Party", "folder"), "a.3": ("Old", "folder"),
              "a.4": ("Dup", "folder"), "a.5": ("New", "folder")}
    rep = immich.build_report(w.snap, disk, chosen)
    by = {r.set_id: r for r in rep.rows}
    assert by["a.1"].severity == "ok" and by["a.1"].albums[0]["name"] == "Summer trip"
    a2 = by["a.2"]
    assert a2.missing_ids == ["22"] and any("no album" in t for _, t in a2.issues)
    a3 = by["a.3"]
    assert any("instead of “Old”" in t for _, t in a3.issues)
    a4 = by["a.4"]
    assert a4.severity == "bad" and any("2 albums" in t for _, t in a4.issues) and a4.offline == 1
    assert by["a.5"].severity == "bad" and "not in Immich" in by["a.5"].issues[0][1]
    assert [a["name"] for a in rep.unlinked] == ["My best of"]
    assert rep.totals["disk"] == 7 and rep.totals["immich"] == 5
    assert rep.rows[-1].set_id == "a.1"          # problems first


def test_newest_first_is_reported(server, keyfile):
    w = immich.ImmichWatcher(server, keyfile)
    w.refresh()
    w.snap.albums[0]["order"] = "desc"
    rep = immich.build_report(w.snap, {"a.1": _disk("Trip (a.1)", ["11", "12"])}, {"a.1": ("Summer trip", "")})
    assert rep.rows[0].severity == "info" and "newest first" in rep.rows[0].issues[0][1]


def test_page(tmp_path, monkeypatch, server, keyfile):
    pytest.importorskip("httpx")
    for k, v in {"DATA_DIR": str(tmp_path), "INTEGRITY": "0", "LOG_FILE": str(tmp_path / "x.log"),
                 "ALBUMS_FILE": str(tmp_path / "x.txt"), "IMMICH_URL": "",
                 "DOWNLOADS_ROOT": str(tmp_path / "dl")}.items():
        monkeypatch.setenv(k, v)
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        assert "Not set up" in c.get("/immich").text
    monkeypatch.setenv("IMMICH_URL", server)
    monkeypatch.setenv("IMMICH_API_KEY_FILE", keyfile)
    m = importlib.reload(m)
    with TestClient(m.app) as c:
        import time
        for _ in range(100):                     # the background thread reads Immich at startup
            if m.IMMICH.snap and not m.IMMICH.busy:
                break
            time.sleep(0.05)
        assert m.IMMICH.snap, m.IMMICH.error
        m.MON.disk = SimpleNamespace(taken=1.0, albums={"a.1": SimpleNamespace(
            folder="Trip (a.1)", media=3, media_ids={"11", "12", "13"}, title="Trip", path=str(tmp_path))})
        html = c.get("/immich").text
        assert "1 photo(s) on disk not in Immich" in html and "13" in html
        assert KEY not in html
        assert c.post("/immich/refresh", headers={"origin": "https://evil.example"}).status_code == 403
