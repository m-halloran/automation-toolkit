# config/rclone: filter rules

Every backup recipe composes its exclusions from the files in `filters/`. Get
this wrong and a sync silently stops protecting something, so the reasoning sits
here rather than in each file.

## Composition

| Variable | Layers, in order |
| --- | --- |
| `filter-from_any` | `any-root-blacklist` |
| `filter-from_c` | `any-root-blacklist` → `volume-root-blacklist` → `c-whitelist` |
| `filter-from_d` | `any-root-blacklist` → `volume-root-blacklist` → `d-blacklist` → `dev-whitelist` |
| `filter-from_e` | `any-root-blacklist` → `volume-root-blacklist` → `e-blacklist` |

## Order is load-bearing

Rules run top-to-bottom within a file, then file-by-file left-to-right, and the
**first match wins**: nothing after that match is consulted.

Deny layers come first, files containing `+` rules last. Put an allowlist first
and its trailing `- **` swallows everything, leaving every later file
unreachable. Its `+` rules would also re-admit junk the shared denylist was meant
to drop: `+ /dev/libavif/**` matches `Thumbs.db` inside that directory and keeps
it unless `any-root-blacklist.txt` has already run.

The credential denies ride on that ordering. They sit atop
`any-root-blacklist.txt`, which every job composes leftmost, so no allowlist can
re-admit a secret however it is written.

## `--filter-from`, not `--exclude-from`

Two of the files need `+` rules, which an exclude-list cannot express, and rclone
warns against mixing the flag families in one command.

They fail in opposite directions. An exclude-list fed to `--filter-from` is
*rejected* for missing `+ `/`- ` prefixes. A filter file fed to `--exclude-from`
**silently matches nothing**, because `- x` reads as a literal pattern starting
with a hyphen and a space. The second is the dangerous one: the job runs clean
and backs up everything you meant to exclude.

## The files

| File | Kind | Applies to |
| --- | --- | --- |
| `any-root-blacklist.txt` | deny | every job; unrooted patterns only |
| `volume-root-blacklist.txt` | deny | volume-root sources only |
| `c-whitelist.txt` | allow | `C:/` |
| `d-blacklist.txt` | deny | `D:/` |
| `dev-whitelist.txt` | allow | `D:/dev`, listed after `d-blacklist.txt` |
| `e-blacklist.txt` | deny | `E:/` |

`C:/` gets an allowlist while the working volumes get denylists. The header of
`c-whitelist.txt` explains why.

## What the exclusions are argued from

Measurement. The two largest categories were excluded for different reasons:

- **Churn, not size.** Caches cost on *every* run. `Cache_Data` measured ~750 MB
  across the Electron apps, `DiskCache` ~270 MB, shader caches ~190 MB, all
  regenerated on demand.
- **File count, not bytes.** `storage/permanent` held ~3,800 files of
  browser-internal IndexedDB and journals, 1,876 of them totalling 0.0 MB in one
  profile. Telemetry added ~1,200 more. These cost checker time and HDD seeks out
  of proportion to their size.

## Rules for editing

- A leading `/` anchors a pattern to the sync source, not the volume. That is why
  the deny layers split in two.
- Forward slashes only. Backslashes can silently fail to match.
- A comment needs its own line.
- Check a new `+` rule cannot shadow the credential denies above it.

Verify with `rclone --dump filters` before running against real data.
