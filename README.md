# gallery-dl dashboard

A self-hosted, read-only web dashboard to supervise a long
[gallery-dl](https://github.com/mikf/gallery-dl) download of Facebook albums:
what it is doing right now, which albums are done, and above all **what may be
missing**.

![Dashboard on demo data](docs/screenshot.png)
*(demo data: every name and ID is invented)*

## What it shows

- **Live header**: downloading / pause before the next album (countdown) /
  rate-limited until hh:mm / blocked / login required / **stalled** (log quiet
  longer than any scheduled pause) / finished with exit code. Current account
  and album, album n/N, new / already-had / missing photos, ETA, downloaded
  size, disk usage and free space, whether the download archive is active.
- **Albums** in `albums.txt` order, each checked for:
  - photos gallery-dl skipped with *"Failed to find photo download URL …
    Skipping."* (the album is still marked done: the most likely silent gap),
  - albums stopped by gallery-dl's *"jump to the beginning"* heuristic,
  - albums marked done whose last pass did not reach the end of the album,
  - photos downloaded per the log but missing on disk, `.part` leftovers,
    media without JSON metadata.
- **Disk check** (read-only, cached, throttled): every photo is opened and
  fully decoded (truncated/corrupt files, HTML pages saved as photos, AVIF data
  in a `.jpg`...); every album is checked for gaps with the `num` positions and
  the `next_photo_id` chain gallery-dl writes in each JSON, which gives the IDs
  of missing photos even for albums downloaded before the log started; JSON
  sidecars are checked against filenames and folders; a photo ID saved twice
  in an album is reported;
  the download archive is compared with the disk (an archived photo that is
  gone would never be downloaded again); and the log is compared with the
  disk: every photo gallery-dl saw must be on disk, and the last pass that
  reached the end of an album gives its full photo list (this also catches a
  photo missing at the very end of an album). An album is "verified" when
  files, JSON and log agree, "verified (disk only)" when it is not in the log.
- **Dates** (read-only): status of a `fix_order.py`-style date fix (mtime +
  EXIF/XMP/QuickTime date in album order) per album: applied / not run /
  partial (new photos) / **changed** (running it now would modify files it had
  already corrected, e.g. after the album changed or with another `--tz`),
  what a run would change, timestamp collisions inside and between albums,
  and whether the album is proven complete enough to be fixed; albums that
  interleave in the timeline as Immich shows it now (EXIF date, else file time). The ordering
  logic is a line-by-line port of the script and was checked against the real
  script's `--dry-run`.
- **Facebook**: upload a saved albums page ("Web Page, Complete", after
  scrolling) to get Facebook's own photo count per album. It is parsed in
  memory and only album IDs, titles and counts are kept. The disk check then
  compares each album with Facebook's count (this also catches photos missing
  at the end of an album), the header shows "seen / expected" and a better
  ETA, Facebook's titles are used everywhere (gallery-dl's can be badly
  decoded), and the Facebook tab lists each page's albums in Facebook's order
  with which range was downloaded and what was skipped in between.
- **Expected and ETA** in the albums table: Facebook's count, checked against
  the photos on disk and the last complete pass (✓ / ✗); for the current and
  pending albums, the estimated end (1 h pause + photos x measured time per
  photo, in albums.txt order), also shown in the header for the current album.
- **Totals and size**: expected photos for all albums in albums.txt (Facebook
  counts, average album size where unknown) vs on disk, and the estimated
  final and remaining size from the average photo size, with a warning when
  the remaining download may not fit on the disk; progress bars for the
  current album, albums, photos, disk usage, each album and each Facebook page.
- **Names**: choose the album name Immich should show (by default
  the Facebook title, else the folder name), kept in
  `album-names.tsv` (data folder, editable by hand too) next to the folder
  name and gallery-dl's title. Folders are never renamed:
  `tools/apply_albumprops.py` (host `python3`, as the downloads' owner) writes immich-folder-album-creator
  `.albumprops` files (`override_name`, plus the Facebook link as `description`,
  `sort_order: asc` and `thumbnail_setting: first`) in every album folder (custom name,
  else Facebook title, else folder name; duplicates get the account or the
  album ID added), dry run by default, only ever touching `.albumprops`
  files and never one it did not write.
- **Immich** (optional, read-only API key): every downloaded album checked
  in Immich: photos on disk not in Immich yet (by photo ID), offline photos,
  photos still in Immich but gone from disk, no album yet, an album with the
  old name instead of the expected one (`.albumprops` not applied), several
  albums for one folder, an album missing photos, newest-first order; plus
  the Immich albums not linked to any folder. Only reads: it lists albums and
  searches assets, never writes. Set `IMMICH_URL` and give the key as a
  Docker secret (`IMMICH_API_KEY_FILE`, default
  `/run/secrets/immich_api_key`, a mode-600 file with only the `album.read`
  and `asset.read` permissions); refreshed every 30 min
  (`IMMICH_REFRESH_SECONDS`) or on demand.
- **Sessions** (one per gallery-dl start): result / exit code, interrupted
  runs (crash, power cut), time spent in each kind of sleep and 429 wait.
- **Events**: every warning and error, with tracebacks; acknowledge the ones
  you have seen (one by one or all at once) so that the header, the menu badge
  and the home page only count new issues (stored in the data folder, stable
  across restarts); **Diagnostics**: log
  lines the parser does not know yet, as privacy-safe templates.

## Privacy

- Read-only: the downloads are mounted `:ro`; nothing is sent anywhere, no
  external CDN, no telemetry.
- Signed image-server URLs (`*.fbcdn.net`, which carry tokens) are never
  stored; facebook.com links are reduced to photo and album IDs.
- Immich (optional) is only read, with a key from a mode-600 file (never an
  environment variable); what is read stays in memory.
- Album names and photos are private: put a login in front (Caddy example
  below). The container publishes no port.

## Layout

```
app/                  the dashboard (FastAPI + Jinja, runs as an unprivileged uid)
  logparse.py         log parser: sessions -> albums -> photos, sleeps, 429s, errors
  status.py           live header (state, ETA, progress) + per-album checks
  disk.py             light read-only disk scan (names, counts, sizes, free space)
  albums.py           albums.txt reader (gallery-dl -I comments finished albums out)
  integrity.py        deep read-only disk check (decode, completeness, duplicates, archive)
  dates.py            read-only fix_order.py status (dates, order, overlaps)
  names.py            album-names.tsv (custom album names) and .albumprops reading
  immich.py           read-only Immich check (albums, assets) against the disk
  fbpages.py          reads a saved Facebook albums page (album IDs, titles, counts)
  fbdata.py           facebook-albums.json (what was extracted, never the page)
Dockerfile            builds the dashboard image (only app/ goes in, see .dockerignore)
tests/                pytest suite, fake-log generator, demo builder
tools/seed_archive.py fill a gallery-dl download archive from files already on disk
tools/apply_albumprops.py write .albumprops (override_name) from album-names.tsv
tools/fix_order.py    sets mtime + EXIF date of each photo in album order (needs exiftool)
deploy/               example compose.yml and gallery-dl.conf
```

Built against gallery-dl **1.32.21** with
`"output": {"logfile": {"level": "debug", "format": "[{asctime}] [{name}][{levelname}] {message}"}}`.

## gallery-dl setup notes

See `deploy/`. Three things matter:

1. **Mount the folder holding `albums.txt`, not the file.** `-I` rewrites the
   file (write + rename); a single-file bind mount keeps showing the old one.
2. **Use a download archive keyed on album + photo ID**:
   `"archive": "...sqlite3", "archive-format": "{set_id}_{id}"`.
   Facebook serves many JPEGs from `.avif` URLs; gallery-dl's "file exists"
   check uses the URL extension, misses the `.jpg` on disk and downloads the
   photo again after every interruption. `tools/seed_archive.py` fills the
   archive from existing files (dry run by default; only complete files with
   valid JSON are added).
3. **Run gallery-dl once per container start** (wrapper in `deploy/compose.yml`):
   it still restarts after a power cut, logs its exit code for the dashboard,
   and does not loop over the list forever.

## Immich (external library) facts this tool relies on

From Immich's server code (`library.service.ts`, `metadata.service.ts`):
an external asset is identified by its **path** (renaming/moving a file or
folder creates a new asset; faces, albums and favorites stay with the old
one); a rescan compares only the **mtime** (when it changed, the asset is
kept but its metadata is re-read and thumbnails, face detection, smart
search, OCR and duplicate detection run again); a content change with an
unchanged mtime is not noticed.

## Deploy

Clone this repo **as the `web-dashboard` folder** next to your `compose.yml`
(the `webui` service has `build: ./web-dashboard`), once:

```bash
git clone https://github.com/lamarch/gallery-dl-dashboard.git web-dashboard
```

```bash
docker compose up -d --build webui
```

Update later (from the compose folder):

```bash
git -C web-dashboard pull && docker compose up -d --build webui
```

Caddy:

```
fbdl.example.com {
    basic_auth {
        someuser <hash from: caddy hash-password>
    }
    reverse_proxy fb-dl-dashboard:8000
}
```

| Variable | Default | Meaning |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | gallery-dl `output.logfile.path` |
| `ALBUMS_FILE` | `/input/albums.txt` | the `-I` file (mount its folder) |
| `DOWNLOADS_ROOT` | `/downloads/facebook` | folder holding the account folders |
| `DISPLAY_TZ` | `TZ`, then UTC | timezone used to display times |
| `LOG_TZ` | `UTC` | timezone of the log timestamps (gallery-dl container time) |
| `BASE_PATH` | empty | e.g. `/fbdl` when served under a sub-path |
| `POLL_SECONDS` / `DISK_SCAN_SECONDS` | 2 / 60 | log polling / disk scan intervals |
| `DATA_DIR` | `/data` | writable folder for the disk-check cache (`integrity.sqlite3`) |
| `ARCHIVE_FILE` | `/downloads/logs/fb-archive.sqlite3` | gallery-dl download archive (read-only) |
| `INTEGRITY` | `1` | `0` turns the disk check off |
| `INTEGRITY_MAX_MBPS` | `40` | disk read cap for the check, in MB/s |
| `INTEGRITY_RESCAN_SECONDS` | `900` | pause between passes (only changed files are re-read) |
| `FIXORDER_TZ` | `DISPLAY_TZ` | the `--tz` you run fix_order.py with (a different one rewrites every EXIF) |
| `FIXORDER_STEP` | `1` | the `--step` you run fix_order.py with |
| `HOST_DOWNLOADS_DIR` / `HOST_DATA_DIR` / `HOST_TOOLS_DIR` | `./downloads` / `./dashboard-data` / `./web-dashboard/tools` | host paths, only used to print ready-to-paste commands |
| `HOST_FIXORDER` | `./web-dashboard/tools/fix_order.py` | where fix_order.py is on the host, for the commands on the Dates tab |

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest -q tests tools/tests
# archive tests against gallery-dl's own code:
GALLERY_DL_SRC=/path/to/gallery-dl-checkout python3 -m pytest -q tools/tests
# demo environment + local server:
python3 tests/make_demo.py /tmp/demo
LOG_FILE=/tmp/demo/logs/fb-dl.log ALBUMS_FILE=/tmp/demo/input/albums.txt \
  DOWNLOADS_ROOT=/tmp/demo/facebook DISK_PATH=/tmp/demo uvicorn app.main:app --port 8000
```
