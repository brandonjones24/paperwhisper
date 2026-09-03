# paperwhisper

**Whispersync-style reading-progress sync between a reMarkable tablet (via [rmfakecloud](https://github.com/ddvk/rmfakecloud)) and [Audiobookshelf](https://www.audiobookshelf.org/).**

You read a book on your reMarkable; paperwhisper nudges the matching audiobook in
Audiobookshelf to roughly the same spot, so you can pick up **listening** where you
left off **reading**. Think "resume where I left off across formats" — a homelab,
self-hosted take on Kindle/Audible Whispersync.

> [!IMPORTANT]
> **This project was vibecoded.** It was designed and written collaboratively with an
> AI assistant (Claude), driven by a homelab owner who is not the original author of
> either rmfakecloud or Audiobookshelf. It works for the author's setup, but it has
> **not** been broadly tested, security-audited, or hardened. Read the code before you
> run it, keep `DRY_RUN=true` until you trust the matches, and treat it as a fun
> weekend integration rather than production software. PRs and issues welcome.

## What it actually does

- Reads your reMarkable **ebook reading position** directly from rmfakecloud's
  on-disk sync store (content-addressed blobs). **Read-only** — it never writes to
  rmfakecloud.
- Matches each ebook to an Audiobookshelf audiobook by fuzzy **title + author**.
- Converts the ebook's `lastOpenedPage / pageCount` into a fraction and sets the
  audiobook's `currentTime = fraction × duration` via the Audiobookshelf API.
- Runs on an interval, only writing when you've actually moved in the book.

## Honest limitations

- **Direction is ebook → audiobook only.** The reverse (audio → ebook) would require
  *rewriting* rmfakecloud's content-addressed sync tree, which is risky enough that
  it's intentionally left out. See [Roadmap](#roadmap).
- **Mapping is percentage-based, not word-accurate.** Real Whispersync uses a
  precomputed audio-to-text alignment; we don't have that. Narration pacing and
  front/back matter differ, so expect to land *near* the right spot (often within a
  page or two of narration), not exactly on it. Chapter-aware mapping is a future idea.
- Page counts on the reMarkable only populate **after a book has been opened** on the
  device, so a book you've never opened won't sync until you do.
- Requires that the **same title exists** as an ebook on the tablet *and* an audiobook
  in Audiobookshelf.

## Requirements

- A running [rmfakecloud](https://github.com/ddvk/rmfakecloud) instance whose data
  directory (`.../data/users/<user>/sync`) is readable by this container.
- [Audiobookshelf](https://www.audiobookshelf.org/) with an API token
  (**Settings → Users → your user → API Token**).
- Docker.

## Quick start

```bash
git clone https://github.com/brandonjones24/paperwhisper.git
cd paperwhisper
cp .env.example .env      # edit RMFAKECLOUD_USER, ABS_URL, ABS_TOKEN
# point the rmdata volume at your rmfakecloud data dir in docker-compose.example.yml
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f
```

Leave `DRY_RUN=true` first. The logs will show each match and the position it *would*
set, e.g.:

```
MATCH 'The Martian: A Novel' <-> 'The Martian' (94%) | ebook 41.2% -> 12894s (abs now 0s, delta 41.2%)
[DRY_RUN] would set 'The Martian' to 12894s
```

When the matches look right, set `DRY_RUN=false` and restart.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `RMFAKECLOUD_DATA` | `/rmdata` | Path to rmfakecloud data dir (mount its host path here, read-only) |
| `RMFAKECLOUD_USER` | — | Your rmfakecloud username (the `users/<user>` folder) |
| `ABS_URL` | — | Audiobookshelf base URL |
| `ABS_TOKEN` | — | Audiobookshelf API token |
| `ABS_VERIFY_TLS` | `true` | Verify TLS to Audiobookshelf |
| `DIRECTION` | `ebook_to_audio` | Only supported value for now |
| `INTERVAL` | `300` | Seconds between passes; `0` = run once |
| `DRY_RUN` | `true` | Log intended changes without writing |
| `MATCH_THRESHOLD` | `0.72` | Fuzzy match cutoff (0–1) |
| `MIN_DELTA` | `0.01` | Minimum fractional move before writing |
| `MIN_PROGRESS` | `0.005` | Ignore ebooks barely started |
| `LOG_LEVEL` | `INFO` | Log verbosity |

## How the reMarkable side works

rmfakecloud stores documents in a content-addressed blob store. paperwhisper walks it
read-only:

```
root                -> hash of the root index blob
<root index>        -> lines: <doc-hash>:80000000:<uuid>:<count>:<size>
<doc-hash>          -> lines: <file-hash>:0:<uuid>.<ext>:<count>:<size>
<uuid>.metadata     -> { "lastOpenedPage", "lastModified", "visibleName", ... }
<uuid>.content      -> { "documentMetadata": {title, authors}, "pageCount", ... }
```

Reading fraction = `lastOpenedPage / pageCount`.

## Roadmap

- Chapter-aware mapping (align by EPUB TOC / audiobook chapters) for better accuracy.
- Optional **audio → ebook** direction (needs safe rmfakecloud sync-tree writes — hard).
- Match caching / manual match overrides.
- Prometheus metrics.

## License

[MIT](LICENSE) © 2026 Brandon Jones. Not affiliated with reMarkable, rmfakecloud, or
Audiobookshelf.
