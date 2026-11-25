"""Build a fake, realistic environment (log + albums.txt + downloads) for demos.

Usage: python3 tests/make_demo.py OUTDIR
Everything is invented. Timestamps end "now" so the header looks live.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakelog import FakeLog  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000 + b"\xff\xd9"

ALBUMS = [  # set_id, account, title, photo count
    ("a.100", "Camille Dupont", "Vacances Bretagne 2019", 40),
    ("a.200", "Camille Dupont", "Été 🌞 Mariage de Léa", 60),
    ("a.300", "Алексей Иванов", "Новый год 2020", 80),
    ("a.400", "Алексей Иванов", "Дача", 30),
    ("a.500", "محمد", "ألبوم العائلة", 25),
    ("a.600", "Camille Dupont", "Profile pictures", 12),
]


def write_photo(root, account, title, sid, pid, date="20250506_082802"):
    d = os.path.join(root, "facebook", account, f"{title} ({sid})")
    os.makedirs(d, exist_ok=True)
    name = f"{date}_{pid}.jpg"
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(JPEG * 50)
    meta = {"id": pid, "set_id": sid, "username": account, "user_id": "1", "user_pfbid": "",
            "caption": "", "date": "2025-05-06 08:28:02", "url": "https://x.fbcdn.net/REDACTED",
            "next_photo_id": "", "filename": "x", "extension": "jpg", "followups_ids": [],
            "num": 1, "title": title, "first_photo_id": "1", "category": "facebook",
            "subcategory": "set"}
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False)


def main(out):
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)
    os.makedirs(os.path.join(out, "input"), exist_ok=True)
    pid = lambda sid, i: str(int(sid[2:]) * 10**8 + i)  # noqa: E731

    # durations are computed so that the log ends ~10 s before now
    now = datetime.now(timezone.utc).replace(microsecond=0)
    f = FakeLog((now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"))
    for _ in range(8):
        f.raw("[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
    f.session_start()
    sid, acc, title, n = ALBUMS[0]
    f.job_start(sid)
    for i in range(1, n + 1):
        if i == 17:
            f.page(pid(sid, i)).no_url_retry(pid(sid, i)).page(pid(sid, i)).no_url_skip(pid(sid, i))
            continue
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i))
    f.end_chain()
    sid, acc, title, n = ALBUMS[1]
    f.job_start(sid)
    for i in range(1, 21):
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i))
    f.at(3 * 3600)  # power cut
    f.session_start()
    f.job_start(sid, archive=True)
    for i in range(1, 21):
        f.skipped(pid(sid, i))
    for i in range(21, n + 1):
        if i == 33:
            f.download_retry(1)
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i))
    f.jump(pid(sid, n), "99999999999")
    sid, acc, title, n = ALBUMS[2]
    # current album: start so that we end near "now"
    f.job_start(sid, archive=True)
    k = n - 30
    for i in range(1, k + 1):
        f.photo(pid(sid, i))
        write_photo(out, acc, title, sid, pid(sid, i))
    f.log("facebook", "debug", "Sleeping 14.20 seconds (request)", 1)
    shift = now.replace(tzinfo=None) - timedelta(seconds=6) - f.t
    lines = []
    for line in f.lines:   # shift every timestamp so the last line is 6 s ago
        if line.startswith("[20"):
            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S") + shift
            line = f"[{ts:%Y-%m-%d %H:%M:%S}]" + line[21:]
        lines.append(line)
    with open(os.path.join(out, "logs", "fb-dl.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    with open(os.path.join(out, "input", "albums.txt"), "w", encoding="utf-8") as fh:
        fh.write("# account 1\n")
        for i, (sid, *_r) in enumerate(ALBUMS):
            url = f"https://www.facebook.com/media/set/?set={sid}&type=3"
            fh.write(("# " if i < 2 else "") + url + "\n")
    print(out)


if __name__ == "__main__":
    main(sys.argv[1])
