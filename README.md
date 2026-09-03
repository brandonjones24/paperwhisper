# paperwhisper

**Whispersync-style reading-progress sync between a reMarkable tablet (via [rmfakecloud](https://github.com/ddvk/rmfakecloud)) and [Audiobookshelf](https://www.audiobookshelf.org/).**

Read a book on your reMarkable and pick up **listening** in Audiobookshelf where you left off — or listen to the audiobook and open the ebook on your reMarkable at roughly the same spot. A homelab, self-hosted take on Kindle/Audible Whispersync.

> [!IMPORTANT]
> **This project was vibecoded.** It was designed and written collaboratively with an
> AI assistant (Claude), driven by a homelab owner who is not the original author of
> either rmfakecloud or Audiobookshelf. It works on the author's setup, but it has
> **not** been broadly tested, security-audited, or hardened. In particular the
> `audio_to_ebook` direction **writes into rmfakecloud's sync store** — read the code,
> keep `DRY_RUN=true` until you trust it, and have a backup. Treat it as a fun weekend
> integration, not production software. Issues and PRs welcome.

## What it does

Two directions (pick one with `DIRECTION`):

| Direction | Reads | Writes | reMarkable side |
|---|---|---|---|
| **`ebook_to_audio`** | reMarkable reading position | Audiobookshelf listen position | read-only |
| **`audio_to_ebook`** | Audiobookshelf listen position | reMarkable reading position | **writes** |

It matches each ebook to an audiobook by fuzzy **title + author**, converts the
source position to a fraction, and maps it onto the target's own scale
(`fraction × audiobook_duration`, or `fraction × ebook_pageCount`).

## Ebook providers

The ebook side is pluggable (`EBOOK_PROVIDER`), so you **don't need a reMarkable** to
use paperwhisper:

| Provider | Reads progress from | Directions |
|---|---|---|
| **`remarkable`** (default) | rmfakecloud sync store (`lastOpenedPage`) | ebook↔audio |
| **`calibreweb`** | Calibre-Web's KOReader **`kosync`** progress (`app.db`) | ebook→audio |

The `calibreweb` provider maps KOReader's per-document reading `percentage` to a
book by recomputing KOReader's *partial-MD5* over your Calibre library files
(`CALIBRE_LIBRARY` may list several libraries, comma-separated; titles/authors come
from each `metadata.db`). So if you read on **KOReader / Kindle / Kobo** synced to
Calibre-Web and listen in Audiobookshelf, your audiobook keeps up with your reading.
It is read-only on the Calibre-Web side (writing progress back is not implemented yet).

## Honest limitations

- **Mapping is percentage-based, not word-accurate.** Real Whispersync uses a
  precomputed audio-to-text alignment; we don't have that. Narration pace and
  front/back matter differ, so you'll land *near* the right spot (often within a page
  or two), not exactly on it. Chapter-aware mapping is on the roadmap.
- **`audio_to_ebook` writes into rmfakecloud's content-addressed sync tree.** This is
  implemented against the reMarkable "sync 1.5" protocol (schema 3 doc indexes hashed
  by Merkle rollup; schema 4 root index hashed by content, with a summary line). It is
  tested and works, but it is the inherently risky half — every write is a
  compare-and-swap on the sync generation, and a bad root is rejected by the tablet
  (recoverable by restoring the prior root). Keep backups.
- **reMarkable page counts populate after the tablet has indexed a book** (which it
  does in the background after syncing — you usually don't need to open each book).
- Requires the **same title** as an ebook (on the tablet) *and* an audiobook (in ABS).

## Requirements

- [rmfakecloud](https://github.com/ddvk/rmfakecloud) whose data directory
  (`.../data/users/<user>/sync`) is readable by this container.
- [Audiobookshelf](https://www.audiobookshelf.org/) with an API token
  (**Settings → Users → your user → API Token**).
- For `audio_to_ebook`: a **device token** (from an `rmapi.conf` registered against
  your rmfakecloud, or set directly).
- Docker.

## Quick start

```bash
git clone https://github.com/brandonjones24/paperwhisper.git
cd paperwhisper
cp .env.example .env      # edit RMFAKECLOUD_USER, ABS_URL, ABS_TOKEN, DIRECTION
# point the rmdata volume at your rmfakecloud data dir in docker-compose.example.yml
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f
```

Leave `DRY_RUN=true` first. The logs show each match and the position it *would* set:

```
# ebook_to_audio
MATCH 'The Martian: A Novel'<->'The Martian' (100%) ebook 41.2% -> 12894s (abs 0s, d41.2%)
[DRY_RUN] would set 'The Martian' to 12894s

# audio_to_ebook
MATCH 'The Martian'<->'The Martian: A Novel' (100%) audio 31.7% -> ebook page 161/508 (was 0)
[DRY_RUN] would set 'The Martian: A Novel' to page 161
```

When the matches look right, set `DRY_RUN=false` and restart.

## Configuration

See [`.env.example`](.env.example). Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `RMFAKECLOUD_DATA` | `/rmdata` | rmfakecloud data dir (mount its host path, read-only) |
| `RMFAKECLOUD_USER` | — | rmfakecloud username (`users/<user>`) |
| `ABS_URL` / `ABS_TOKEN` | — | Audiobookshelf base URL + API token |
| `DIRECTION` | `ebook_to_audio` | `ebook_to_audio` or `audio_to_ebook` |
| `RMFAKECLOUD_URL` | — | rmfakecloud HTTP API (required for `audio_to_ebook`) |
| `RMFAKECLOUD_DEVICE_TOKEN` / `RMAPI_CONFIG` | — | device token for writing (required for `audio_to_ebook`) |
| `INTERVAL` | `300` | Seconds between passes; `0` = run once |
| `DRY_RUN` | `true` | Log intended changes without writing |
| `MATCH_THRESHOLD` | `0.72` | Fuzzy match cutoff (0–1) |
| `MIN_DELTA` | `0.01` | `ebook_to_audio`: min fractional move before writing |
| `MIN_PAGE_DELTA` | `1` | `audio_to_ebook`: min page move before writing |

## How the reMarkable side works

rmfakecloud stores documents in a content-addressed blob store; paperwhisper walks it:

```
root                -> hash of the root index blob
<root index>        -> "4" + summary "0:.:<count>:<size>" + doc lines
<doc-hash>          -> "3" + file lines <file-hash>:0:<uuid>.<ext>:0:<size>
<uuid>.metadata     -> { "lastOpenedPage", "lastModified", ... }
<uuid>.content      -> { "documentMetadata": {title, authors}, "pageCount", ... }
```

- Reading progress = `lastOpenedPage / pageCount`.
- **Hashing (verified on a live store):** leaf = `sha256(content)`; doc index (schema 3)
  = Merkle rollup of child hashes (sorted by name); root index (schema 4) =
  `sha256(root content)`.
- Writes use the sync API: exchange the device token for a user token, `GET/PUT
  /sync/v3/files/:hash` for blobs, `PUT /sync/v3/root` (CAS on generation, with
  `Broadcast` so the tablet re-syncs).

## Roadmap

- Chapter-aware mapping (EPUB TOC ↔ audiobook chapters) for better accuracy.
- Write-back for the `calibreweb` provider (audio → Calibre-Web reading position).
- Hash-index caching for large Calibre libraries; manual match overrides.
- Prometheus metrics.

## License

[MIT](LICENSE) © 2026 Brandon Jones. Not affiliated with reMarkable, rmfakecloud, or
Audiobookshelf.
