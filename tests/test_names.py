import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

from app.names import NamesFile, albumprops_content, clean_name, read_albumprops  # noqa: E402
import apply_albumprops as tool  # noqa: E402

WEIRD = [
    "Été 🌞 Mariage de Léa",
    "Новый год 2020",
    "ألبوم العائلة‏",                  # right-to-left mark kept
    "Family 👨‍👩‍👧",            # emoji with zero-width joiners kept
    'Quotes "double" and \'single\' # not a comment: colon',
    "back\\slash",
]


def test_clean_name():
    for w in WEIRD:
        assert clean_name(w) == w.strip()
    assert clean_name("  a \t b\n c ") == "a b c"
    assert clean_name("x\x00y\x07") == "xy"
    with pytest.raises(ValueError):
        clean_name("x" * 201)


def test_file_roundtrip_and_hand_edits(tmp_path):
    p = str(tmp_path / "album-names.tsv")
    nf = NamesFile(p)
    nf.sync({"a.1": ("Vacances (a.1)", "Vacances"), "a.2": ("Tab\there (a.2)", "x")}, write=True)
    nf.set_custom("a.1", WEIRD[4])
    again = NamesFile(p)
    again.load()
    assert again.rows["a.1"].custom_name == WEIRD[4]
    assert again.rows["a.2"].folder == "Tab\there (a.2)"          # escaped tab survives
    assert again.rows["a.1"].folder_title == "Vacances"
    # someone edits the file by hand, then a name is saved in the UI: both survive
    txt = open(p, encoding="utf-8").read().replace("\tx\t\t\n", "\tx\t\tHand made\n")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(txt)
    os.utime(p, (1, 1))
    nf.set_custom("a.1", "From UI")
    final = NamesFile(p)
    final.load()
    assert final.rows["a.2"].custom_name == "Hand made" and final.rows["a.1"].custom_name == "From UI"
    assert os.path.exists(p + ".bak")
    # sync never touches custom names
    nf.sync({"a.1": ("Vacances renamed (a.1)", "Vacances")}, write=True)
    assert nf.rows["a.1"].custom_name == "From UI"


def test_duplicates(tmp_path):
    nf = NamesFile(str(tmp_path / "n.tsv"))
    nf.sync({"a.1": ("A (a.1)", ""), "a.2": ("B (a.2)", "")}, write=True)
    nf.set_custom("a.1", "Same")
    nf.set_custom("a.2", "same")
    assert nf.duplicates() == {"same": ["a.1", "a.2"]}


def test_albumprops_is_valid_yaml_for_the_album_creator():
    yaml = pytest.importorskip("yaml")
    for w in WEIRD:
        assert yaml.safe_load(albumprops_content(w, "a.12")) == {
            "override_name": w, "description": "Facebook album: https://www.facebook.com/media/set/?set=a.12",
            "sort_order": "asc", "thumbnail_setting": "first"}


def _lib(tmp_path):
    root = tmp_path / "facebook"
    for sid, title in (("a.1", "Vacances"), ("a.2", "Новый год"), ("a.3", "Foreign")):
        d = root / "Acc" / f"{title} ({sid})"
        d.mkdir(parents=True)
        (d / "20240101_000000_1.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    (root / "Acc" / "Foreign (a.3)" / ".albumprops").write_text("override_name: \"theirs\"\n", encoding="utf-8")
    names = tmp_path / "album-names.tsv"
    nf = NamesFile(str(names))
    nf.sync({"a.1": ("Vacances (a.1)", ""), "a.2": ("Новый год (a.2)", ""),
             "a.3": ("Foreign (a.3)", ""), "a.9": ("Gone (a.9)", "")}, write=True)
    nf.set_custom("a.1", WEIRD[0])
    nf.set_custom("a.3", "Mine")
    nf.set_custom("a.9", "No folder")
    return root, names


def snapshot(root):
    return {os.path.join(dp, n): open(os.path.join(dp, n), "rb").read()
            for dp, _, ns in os.walk(root) for n in ns}


def test_tool_dry_run_writes_nothing(tmp_path, capsys):
    root, names = _lib(tmp_path)
    before = snapshot(root)
    assert tool.main(["--root", str(root), "--names", str(names)]) == 0
    assert snapshot(root) == before
    out = capsys.readouterr().out
    assert "write" in out and "skip" in out and "missing" in out and "DRY RUN" in out


def test_tool_write_only_albumprops(tmp_path):
    root, names = _lib(tmp_path)
    before = snapshot(root)
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    after = snapshot(root)
    changed = {p for p in after if before.get(p) != after[p]}
    # every album folder gets one (except a.3: someone else's .albumprops)
    assert changed == {str(root / "Acc" / "Vacances (a.1)" / ".albumprops"),
                       str(root / "Acc" / "Новый год (a.2)" / ".albumprops")}
    assert read_albumprops(str(root / "Acc" / "Vacances (a.1)")) == ("ours", WEIRD[0])
    assert after[str(root / "Acc" / "Vacances (a.1)" / ".albumprops")].decode() == albumprops_content(WEIRD[0], "a.1")
    assert read_albumprops(str(root / "Acc" / "Новый год (a.2)")) == ("ours", "Новый год")   # folder name
    assert read_albumprops(str(root / "Acc" / "Foreign (a.3)")) == ("foreign", "theirs")
    # idempotent
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    assert snapshot(root) == after
    # custom name removed: back to the folder name
    nf = NamesFile(str(names))
    nf.load()
    nf.set_custom("a.1", "")
    tool.main(["--root", str(root), "--names", str(names), "--write"])
    assert read_albumprops(str(root / "Acc" / "Vacances (a.1)")) == ("ours", "Vacances")
    assert read_albumprops(str(root / "Acc" / "Foreign (a.3)")) == ("foreign", "theirs")


def test_names_priority_and_duplicates_match_the_dashboard(tmp_path):
    from app.names import NameRow, album_names
    root = tmp_path / "facebook"
    spec = [("A1", "Profile pictures (a.1)", "", ""),       # same folder name in two accounts
            ("A2", "Profile pictures (a.2)", "", ""),
            ("A1", "Trip (a.3)", "Summer 2019", ""),         # Facebook title
            ("A1", "Party (a.4)", "Summer 2019", "Party!"),  # custom beats Facebook
            ("A1", "x (a.5)", "Same", ""),                   # same name, same account
            ("A1", "y (a.6)", "same", "")]
    rows, paths = [], {}
    for acc, folder, fb, custom in spec:
        d = root / acc / folder
        d.mkdir(parents=True)
        sid = folder[folder.index("(") + 1:-1]
        rows.append(NameRow(sid, folder, "", fb, custom))
        paths[sid] = str(d)
    nf = NamesFile(str(tmp_path / "n.tsv"))
    nf.rows = {r.set_id: r for r in rows}
    nf._write()
    want = {"a.1": ("Profile pictures (A1)", "folder+account"), "a.2": ("Profile pictures (A2)", "folder+account"),
            "a.3": ("Summer 2019", "facebook"), "a.4": ("Party!", "custom"),
            "a.5": ("Same (a.5)", "facebook+id"), "a.6": ("same (a.6)", "facebook+id")}
    assert album_names(rows, paths) == want
    got = tool.choose_names(tool.album_folders(str(root)), tool.read_names(str(tmp_path / "n.tsv")))
    assert {sid: (n, src) for f, (n, src, sid) in got.items()} == want


def test_routes_refuse_cross_site_posts(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    root, names = _lib(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOADS_ROOT", str(root))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "none.log"))
    monkeypatch.setenv("ALBUMS_FILE", str(tmp_path / "none.txt"))
    monkeypatch.setenv("INTEGRITY", "0")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        assert c.get("/names").status_code == 200
        r = c.post("/names/a.2", data={"custom_name": "Evil"},
                   headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
        assert r.status_code == 403
        r = c.post("/names/a.2", data={"custom_name": "Réveillon"},
                   headers={"sec-fetch-site": "same-origin"}, follow_redirects=False)
        assert r.status_code == 303
        assert "Réveillon" in c.get("/names").text
        assert c.post("/integrity/rescan", headers={"origin": "https://evil.example"}).status_code == 403


def test_ignore_route(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    root, _ = _lib(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOWNLOADS_ROOT", str(root))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "none.log"))
    monkeypatch.setenv("ALBUMS_FILE", str(tmp_path / "none.txt"))
    monkeypatch.setenv("INTEGRITY", "0")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        h = {"sec-fetch-site": "same-origin"}
        assert c.post("/album/a.1/ignore", data={"photo_id": "1x"}, headers=h).status_code == 400
        assert c.post("/album/a.1/ignore", data={"photo_id": "123"},
                      headers={"sec-fetch-site": "cross-site"}).status_code == 403
        r = c.post("/album/a.1/ignore", data={"photo_id": "123", "reason": "autre\talbum"},
                   headers=h, follow_redirects=False)
        assert r.status_code == 303
        assert m.IGNORES.snapshot() == {"a.1": {"123": "autre album"}}
        c.post("/album/a.1/ignore", data={"photo_id": "123", "undo": "1"}, headers=h)
        assert m.IGNORES.snapshot() == {}


def test_fixorder_command_is_shell_safe(tmp_path, monkeypatch):
    import shlex
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTEGRITY", "0")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "none.log"))
    monkeypatch.setenv("HOST_DOWNLOADS_DIR", "./dl dir")
    monkeypatch.setenv("FIXORDER_TZ", "Europe/Berlin")
    monkeypatch.setenv("FIXORDER_STEP", "0.1")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    evil = "Acc/L'été $(rm -rf ~) `x` 🎄 \\ (a.1)"
    cmd = m.fixorder_cmd([evil, "Acc/Новый год (a.2)"], dry=True)
    assert shlex.split(cmd) == ["python3", "./web-dashboard/tools/fix_order.py", "--tz", "Europe/Berlin", "--step", "0.1",
                                "--dry-run", f"./dl dir/facebook/{evil}", "./dl dir/facebook/Acc/Новый год (a.2)"]
    assert "--dry-run" not in m.fixorder_cmd([evil], dry=False)


def test_albumprops_command(tmp_path, monkeypatch):
    import shlex
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTEGRITY", "0")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "none.log"))
    monkeypatch.setenv("HOST_DOWNLOADS_DIR", "./my dl's")
    import importlib
    import app.main as m
    m = importlib.reload(m)
    dry, write = shlex.split(m.albumprops_cmd(False)), shlex.split(m.albumprops_cmd(True))
    assert dry == ["python3", "./web-dashboard/tools/apply_albumprops.py", "--root", "./my dl's/facebook",
                   "--names", "./dashboard-data/album-names.tsv"]
    assert write == dry + ["--write"]


def test_static_files_are_versioned(tmp_path, monkeypatch):
    """Browsers must not keep an old app.js/style.css after an update."""
    pytest.importorskip("httpx")
    for k, v in {"DATA_DIR": str(tmp_path), "INTEGRITY": "0", "LOG_FILE": str(tmp_path / "x.log"),
                 "ALBUMS_FILE": str(tmp_path / "x.txt")}.items():
        monkeypatch.setenv(k, v)
    import hashlib
    import importlib
    import re
    import app.main as m
    m = importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(m.app) as c:
        html = c.get("/names").text
        for name in ("app.js", "style.css"):
            mm = re.search(rf'/static/{re.escape(name)}\?v=([0-9a-f]+)"', html)
            assert mm, name
            body = open(os.path.join(HERE, "..", "app", "static", name), "rb").read()
            assert mm.group(1) == hashlib.sha1(body).hexdigest()[:10]
            assert c.get(f"/static/{name}?v={mm.group(1)}").status_code == 200


def test_fill_from_facebook_keeps_chosen_names(tmp_path):
    nf = NamesFile(str(tmp_path / "n.tsv"))
    nf.sync({"a.1": ("Vacances (a.1)", "Vacances"), "a.2": ("Ete (a.2)", "Ete"),
             "a.3": ("Same (a.3)", "Same")}, write=True,
            fb_titles={"a.1": "Vacances 2019 🌞", "a.2": "Été", "a.3": "Same"})
    nf.set_custom("a.2", "Mon choix")
    assert nf.fill_from_facebook() == 1                  # a.1 only
    assert nf.rows["a.1"].custom_name == "Vacances 2019 🌞"
    assert nf.rows["a.2"].custom_name == "Mon choix"     # never overwritten
    assert nf.rows["a.3"].custom_name == ""              # same as the folder: nothing to rename
