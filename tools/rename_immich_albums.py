#!/usr/bin/env python3
"""Rename existing Immich albums to the names chosen in album-names.tsv.

immich-folder-album-creator finds albums BY NAME: once .albumprops gives an
album a new name (override_name), the tool would create a second album
instead of reusing the old one. Renaming the old album first keeps it, with
everything attached to it (description, sharing, cover, comments); the photos
themselves are never touched either way.

Safety:
  * DRY RUN by default: prints the plan. --write to rename.
  * Only renames (PATCH /api/albums/<id> {"albumName": ...}); never deletes,
    never adds or removes photos.
  * An album is matched only when its cover photo lives in that album's
    folder AND its name contains the folder name (or the folder name without
    " (a.<id>)"), whatever naming options the album creator uses; albums
    made by hand are never renamed.
  * Skips when several albums match, when the cover cannot be checked, or
    when an album with the new name already exists (shown as a conflict).

The API key is read from a file (--api-key-file, which must not be readable
by other users: chmod 600) or from the IMMICH_API_KEY environment variable;
never from the command line, so it does not end up in your shell history. It
needs the album.read, album.update and asset.read permissions.

Usage:
  python3 rename_immich_albums.py --url https://immich.example \\
      --api-key-file rename_albums.immich_api_key \\
      --names dashboard-data/album-names.tsv [--write]
Standard library only.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

COLUMNS = ("set_id", "folder", "gallerydl_title", "facebook_title", "custom_name")


def unesc(v):
    out, i = [], 0
    while i < len(v):
        if v[i] == "\\" and i + 1 < len(v):
            out.append({"t": "\t", "n": "\n", "\\": "\\"}.get(v[i + 1], "\\" + v[i + 1]))
            i += 2
        else:
            out.append(v[i])
            i += 1
    return "".join(out)


def read_names(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    header = lines[0].split("\t")
    idx = {c: header.index(c) for c in COLUMNS if c in header}
    if "set_id" not in idx or "custom_name" not in idx or "folder" not in idx:
        sys.exit(f"{path}: missing columns (set_id, folder, custom_name)")
    rows = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        get = lambda c: unesc(p[idx[c]]).strip() if c in idx and idx[c] < len(p) else ""  # noqa: E731
        if get("set_id") and get("custom_name"):
            rows.append({"set_id": get("set_id"), "folder": get("folder"), "new": get("custom_name")})
    return rows


class Immich:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/api"
        self.key = key

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"x-api-key": self.key, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if data else {})})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else None


SID_IN_PATH = re.compile(r"\((a\.\d+)\)\s*$")


def _norm(v):
    return " ".join((v or "").casefold().split())


def plan(api, rows):
    """Match each album of album-names.tsv to ONE Immich album:
      * its cover photo lives in that album's folder ("... (a.<id>)"), and
      * its name contains the folder name, or the folder name without
        " (a.<id>)" (whatever ALBUM_LEVELS / separator / regex the album
        creator uses, its names are built from the folder names).
    Albums made by hand (name unrelated to the folder) are never renamed."""
    albums = api.call("GET", "/albums")
    by_name = {}
    for a in albums:
        by_name.setdefault(a.get("albumName"), []).append(a)
    # where does each album's cover live?
    by_cover = {}
    covers = {}
    for a in albums:
        thumb = a.get("albumThumbnailAssetId")
        if not thumb:
            continue
        if thumb not in covers:
            try:
                covers[thumb] = (api.call("GET", f"/assets/{thumb}") or {}).get("originalPath")
            except urllib.error.HTTPError:
                covers[thumb] = None
        path = covers[thumb]
        if path:
            m = SID_IN_PATH.search(path.replace("\\", "/").rsplit("/", 1)[0])
            if m:
                by_cover.setdefault(m.group(1), []).append(a)
    actions = []   # (action, set_id, detail, album_id, new_name)
    taken = {_norm(n) for n in by_name}
    for r in rows:
        sid, folder, new = r["set_id"], r["folder"], r["new"]
        bare = re.sub(r"\s*\([^()]+\)\s*$", "", folder)
        if new in (folder, bare):
            continue
        here = by_cover.get(sid, [])
        keys = [k for k in (_norm(folder), _norm(bare)) if k]
        plausible = [a for a in here if any(k in _norm(a.get("albumName")) for k in keys)]
        if any(a.get("albumName") == new for a in here):
            if plausible:   # the old album AND one with the new name: a duplicate to sort out
                actions.append(("conflict", sid, f"both “{plausible[0]['albumName']}” and “{new}” exist "
                                                 f"(the album creator made a second album?): rename skipped",
                                None, None))
            else:
                actions.append(("ok", sid, f"already named “{new}”", None, None))
            continue
        if len(plausible) > 1:
            actions.append(("skip", sid, f"{len(plausible)} albums have their cover in this folder and a name "
                                         f"like it: {', '.join('“%s”' % a['albumName'] for a in plausible)}", None, None))
            continue
        if not plausible:
            if here:
                actions.append(("skip", sid, f"album “{here[0]['albumName']}” has its cover in this folder but "
                                             f"its name does not come from the folder: not renamed", None, None))
            elif by_name.get(folder) or by_name.get(bare):
                a = (by_name.get(folder) or by_name.get(bare))[0]
                actions.append(("skip", sid, f"album “{a['albumName']}” is named like the folder but its cover "
                                             f"is elsewhere: not renamed", None, None))
            else:
                actions.append(("none", sid, f"no Immich album with its cover in “{folder}” "
                                             f"(not created yet, or its cover is in another folder)", None, None))
            continue
        a = plausible[0]
        if _norm(new) in taken:
            actions.append(("conflict", sid, f"an album named “{new}” already exists "
                                             f"(created by the album creator?): rename skipped", None, None))
            continue
        actions.append(("rename", sid, f"“{a['albumName']}” → “{new}”", a["id"], new))
        taken.add(_norm(new))                       # two folders can't take the same new name
    return actions


def read_key_file(path):
    """API key from a file that only its owner can read."""
    import stat
    try:
        st = os.stat(path)
    except OSError as e:
        sys.exit(f"cannot read the API key file: {e.strerror}")
    if os.name == "posix" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"{path} can be read by other users: run  chmod 600 {path}  first")
    with open(path, encoding="utf-8") as fh:
        key = fh.read().strip()
    if not key or any(c.isspace() for c in key):
        sys.exit(f"{path} must contain the API key only")
    return key


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rename Immich albums to the names in album-names.tsv "
                                             "(dry run unless --write).")
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich URL (or IMMICH_URL)")
    ap.add_argument("--names", required=True, help="album-names.tsv from the dashboard data folder")
    ap.add_argument("--api-key-file", help="file holding the API key (chmod 600)")
    ap.add_argument("--write", action="store_true", help="really rename")
    args = ap.parse_args(argv)
    key = read_key_file(args.api_key_file) if args.api_key_file else os.environ.get("IMMICH_API_KEY")
    if not args.url or not key:
        sys.exit("set --url (or IMMICH_URL) and --api-key-file (or the IMMICH_API_KEY environment variable)")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    api = Immich(args.url, key)
    try:
        actions = plan(api, read_names(args.names))
    except urllib.error.HTTPError as e:
        sys.exit(f"Immich answered {e.code}: check the URL and the API key permissions")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach Immich: {e.reason}")
    counts = {}
    for act, sid, detail, _, _ in actions:
        counts[act] = counts.get(act, 0) + 1
        print(f"{act:9} {sid:24} {detail}")
    print("summary:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")
    if not args.write:
        print("DRY RUN: nothing renamed. Re-run with --write to apply.")
        return 0
    done = 0
    for act, sid, detail, album_id, new in actions:
        if act == "rename":
            api.call("PATCH", f"/albums/{album_id}", {"albumName": new})
            done += 1
    print(f"renamed: {done} album(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
