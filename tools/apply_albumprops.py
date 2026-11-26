#!/usr/bin/env python3
"""Write immich-folder-album-creator `.albumprops` files from album-names.tsv.

Gives albums the custom names chosen in the dashboard WITHOUT renaming any
folder (an Immich external asset is identified by its path: renaming a
folder creates new assets and loses faces, albums and favorites).

Safety:
  * DRY RUN by default: prints the plan, writes nothing. --write to apply.
  * Only ever creates/replaces/removes files named `.albumprops`; never
    touches photos, JSON sidecars or folders.
  * A `.albumprops` not written by this tool (no marker line) is never
    modified or removed.
  * Writes are atomic (temporary file + rename in the same folder).
  * Albums are found by the "(set_id)" at the end of the folder name.

Immich side: set READ_ALBUM_PROPERTIES=1 on immich-folder-album-creator.
Albums already created under the old name are not renamed by that tool (it
matches albums by name and would create a new one): rename the existing album
in Immich to the new name first, and the tool reuses it.

Standard library only.
"""
import argparse
import json
import os
import re
import sys

MARKER = "# managed by gallery-dl-dashboard"
SID_IN_DIR = re.compile(r"\(([^()]+)\)\s*$")
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
    if "set_id" not in header or "custom_name" not in header:
        sys.exit(f"{path}: missing header (set_id ... custom_name)")
    i_sid, i_name = header.index("set_id"), header.index("custom_name")
    names = {}
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        p = line.split("\t")
        sid = unesc(p[i_sid]).strip() if i_sid < len(p) else ""
        name = unesc(p[i_name]).strip() if i_name < len(p) else ""
        if sid:
            names[sid] = name
    return names


def content(name):
    return (f"{MARKER} (tools/apply_albumprops.py): album name for immich-folder-album-creator\n"
            f"override_name: {json.dumps(name, ensure_ascii=False)}\n")


def album_folders(root):
    out = {}
    for acc in sorted(os.listdir(root)):
        ap = os.path.join(root, acc)
        if not os.path.isdir(ap):
            continue
        for alb in sorted(os.listdir(ap)):
            p = os.path.join(ap, alb)
            m = SID_IN_DIR.search(alb)
            if m and os.path.isdir(p):
                out.setdefault(m.group(1), []).append(p)
    return out


def plan(root, names, remove_stale):
    actions = []      # (action, path, detail)
    folders = album_folders(root)
    for sid, paths in sorted(folders.items()):
        want = names.get(sid, "")
        for folder in paths:
            p = os.path.join(folder, ".albumprops")
            exists = os.path.exists(p)
            cur = open(p, encoding="utf-8").read() if exists else None
            ours = cur is not None and cur.startswith(MARKER)
            if want:
                if exists and not ours:
                    actions.append(("skip", p, "a .albumprops not written by this tool exists: left untouched"))
                elif cur == content(want):
                    actions.append(("ok", p, want))
                else:
                    actions.append(("write", p, want))
            elif ours:
                actions.append(("remove" if remove_stale else "stale", p,
                                "no custom name any more" + ("" if remove_stale else " (use --remove-stale)")))
    unknown = sorted(set(s for s, n in names.items() if n) - set(folders))
    for sid in unknown:
        actions.append(("missing", sid, "custom name for an album with no folder on disk"))
    return actions


def write_atomic(path, text):
    tmp = os.path.join(os.path.dirname(path), ".albumprops.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write .albumprops (override_name) from album-names.tsv. "
                                             "Dry run unless --write.")
    ap.add_argument("--root", required=True, help="e.g. /downloads/facebook")
    ap.add_argument("--names", required=True, help="e.g. /data/album-names.tsv")
    ap.add_argument("--write", action="store_true", help="apply the plan")
    ap.add_argument("--remove-stale", action="store_true",
                    help="remove .albumprops written by this tool whose album has no custom name any more")
    args = ap.parse_args(argv)
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    actions = plan(args.root, read_names(args.names), args.remove_stale)
    counts = {}
    for act, path, detail in actions:
        counts[act] = counts.get(act, 0) + 1
        if act != "ok":
            print(f"{act:8} {path}  {detail}")
    print("summary:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do")
    if not args.write:
        print("DRY RUN: nothing written. Re-run with --write to apply.")
        return 0
    done = 0
    for act, path, detail in actions:
        if act == "write":
            write_atomic(path, content(detail))
            done += 1
        elif act == "remove":
            with open(path, encoding="utf-8") as fh:
                if fh.read().startswith(MARKER):       # re-check right before removing
                    os.remove(path)
                    done += 1
    print(f"applied: {done} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
