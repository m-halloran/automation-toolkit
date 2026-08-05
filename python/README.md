# python: Python utilities

| Script | Reached by | What it does |
| --- | --- | --- |
| `sync_drift.py` | `r2 drift` | Reconciles a destination against its source. When files are reorganised at the source, a naive `sync` deletes and re-copies them: hours of transfer to achieve a rename. This walks both trees, matches by size and modification window (optionally by hash), and moves files within the destination instead, so the mirror ends up correct having transferred nothing. Journalled, with `--undo`. |
| `media_info.py` | `r2 media-info` | Reports codec, resolution, framerate and bitrate for media under a directory. |
| `collect_files.py` | `r2 py-move` | Collects files matching given extensions into a flat destination, auto-renaming on collision. Interactive input tolerates pasted quotes and missing dots. |

## `sync_drift.py`

The largest tool here at ~1,200 lines, and the one that best states the theme.

- It only ever moves files *within* the destination, and never reads or writes
  the source tree. No file is deleted. The one removal it does is
  `--prune-empty-dirs`, on directories its own moves left empty, and it refuses
  to remove the destination root.
- Cross-volume moves are refused unless `--allow-cross-volume` is passed. A move
  across volumes is a copy-and-delete, not a rename, and the failure modes differ.
- Every run is journalled to JSON with the before and after path of each move.
  `--undo` replays a journal backwards.
- `--dry-run` is the intended first step; `--show-diff` prints what would change
  without touching anything.
- `--verify` upgrades matching from size-and-modtime to content hash, bounded by
  `--max-hash-size` so a multi-gigabyte file does not force a full read.

It takes exclusion rules in both formats: bare patterns via `--exclude`, and rule
files via `--exclude-from`, which strips the `- ` prefix so an rclone filter file
passes straight through. One set of rules drives both tools.

An allowlist file is **refused**, not partially honoured. The matcher is
exclusion-only and cannot express "keep this despite a later deny", so it would
have to drop the `+` rules while still obeying the `- **` that follows them,
excluding the entire tree and reporting zero drift. Wrong, and silently so.
