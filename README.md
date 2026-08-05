# automation-toolkit

Windows 11 workstation automation. A [`just`](https://github.com/casey/just)
command layer drives backup, video encoding and system reporting across a
multi-drive workstation, delegating to PowerShell and Python where each fits.

Self-taught from no prior programming experience (Aug 2025 – present). A
selection of the tooling I run daily, not a framework or a template.

The recurring theme is **data integrity**. Workflows verify before they trust,
fail closed on a mismatch, version everything they overwrite, and log everything
they do.

## What's here

| Capability | Where | What it does |
| --- | --- | --- |
| Backup web | [`just/r2/justfile`](just/r2/justfile) (`sync`, `sf`, `sg`, `drv`) | Rename-aware mirroring of three volumes to two rotating cold-storage disks and a cloud mount, behind mount-presence guards. |
| Exclusion rules | [`config/rclone/filters/`](config/rclone/filters) | The layered filter architecture every backup job depends on. |
| AV1 archival | [`just/r2/justfile`](just/r2/justfile) (`av1`, `av1-q`, `av1-r`, `check-vfr`, `ffav1`) | Chunked AV1 encoding with frame-exact timing correction for variable-framerate sources. |
| Encoder measurement | [`pwsh/Measure-Av1Crf.ps1`](pwsh/Measure-Av1Crf.ps1) | Sample-encodes clips to find the CRF that hits a VMAF target, and measures throughput to project encode time. |
| Drift reconciliation | [`python/sync_drift.py`](python/sync_drift.py) | Realigns a destination's layout by renaming in place rather than re-copying. Journalled, with `--undo`. |
| Reporting | [`pwsh/Get-SystemSpecs.ps1`](pwsh/Get-SystemSpecs.ps1), [`pwsh/Get-Index.ps1`](pwsh/Get-Index.ps1), [`python/media_info.py`](python/media_info.py) | Hardware and live per-disk I/O, numbered directory snapshots, codec and bitrate reporting. |

## Design principles

**Fail closed.** The encode pipeline counts every frame in the source and the
output. If they differ by one it aborts, keeps the intermediate for inspection,
and produces nothing. The drive guards halt a sync rather than back up to a disk
that isn't there.

**Verify, then trust.** The `C:/` legs compare rename-tracked hashes, not
timestamps. `--check-first` scans before a byte transfers; `--delete-after` means
an interrupted run never leaves the destination worse than it started.

**Version everything destructive.** Cloud syncs pass `--backup-dir`, so anything
overwritten or deleted moves into a dated archive. The justfile is snapshotted by
content hash: a timestamped copy is written only when its SHA-256 differs from
the last, so no change is lost and an unchanged file costs nothing.

**Log everything.** Transfers write to a rotating structured log (100 MB × 15
files) under `D:/code/var/logs/`, so any past run can be audited.
[`examples/sample-run.log`](examples/sample-run.log) shows the shape of it.

**Ask before deleting.** `proxy` scans for orphaned proxy files, cross-references
each against its master, prints what would go and why, then acts only on an
explicit `y/N`. The default is no.

## Repository layout

Mirrors `D:\code` on the host, so the absolute paths inside recipes line up with
the directories here.

| Path | Contents |
| --- | --- |
| `just/r2/` | The command layer. `justfile` defines every recipe. |
| `config/rclone/filters/` | Filter rules consumed by the backup recipes. Strictly no configuration, remotes or credentials. |
| `pwsh/` | PowerShell utilities called by recipes, plus standalone tools. |
| `python/` | Python utilities called by recipes. |
| `examples/` | An illustrative log showing the format a run leaves behind. |

## Command layer: `just`

The justfile pins its shell to an absolute path:

```just
set shell := ["D:/dev/git/bin/bash.exe", "-cu"]
```

On Windows a bare `bash` resolves to the WSL launcher in `System32`, which would
run every recipe inside a Linux VM where `D:/` does not exist. The absolute path
guarantees the intended environment. `-cu` enables `nounset`, so an unset variable
is an error rather than an empty string; `errexit` is not, so a failing command
does not by itself abort a recipe.

`_check-drives` takes `LETTER:Name` pairs, reports mount status, and halts the
caller with `exit 1` unless confirmed, so a disconnected disk stops the run
instead of producing a partial backup that looks complete. Recipes prefixed `_`
are internal helpers.

## Filter architecture

Each backup job composes its exclusions from layered filter files, and the
**first match wins**, so the order they are listed in is the behaviour. Get it
wrong and an allowlist's trailing `- **` silently swallows every later file.

[`config/rclone/README.md`](config/rclone/README.md) has the full reasoning.
Read it before editing a filter.

## Storage layout

| Letter | Type | Role |
| --- | --- | --- |
| `C:` | M.2 NVMe SSD | System volume, user profile, application data |
| `D:` | M.2 NVMe SSD | Tooling, code, working media |
| `E:` | 4 TB hard disk | Bulk storage, cloud-sync mount at `E:/Drive` |
| `F:` `G:` | Hard disk | Rotating cold-storage backup targets, connected only during a run |

## Requirements

Grouped by what needs them.

| For | Tools |
| --- | --- |
| Everything | [just](https://github.com/casey/just) · [Git for Windows](https://gitforwindows.org/) (git-bash) · [PowerShell 7](https://github.com/PowerShell/PowerShell) |
| Backup | [rclone](https://rclone.org/) |
| Video | [av1an](https://github.com/rust-av/Av1an) · [SVT-AV1](https://gitlab.com/AOMediaCodec/SVT-AV1) · [ffmpeg/ffprobe](https://ffmpeg.org/) · [mkvmerge](https://mkvtoolnix.download/) |
| Images | [cjxl](https://github.com/libjxl/libjxl) · [avifenc](https://github.com/AOMediaCodec/libavif) · [ImageMagick](https://imagemagick.org/) |
| Utilities | [Python 3.12](https://www.python.org/) |

PowerShell sits in the first row because every capability shells out to it.

## Usage

Two functions in the PowerShell profile do the wrapping, so no directory change
or `--justfile` flag is needed:

```powershell
function r2  { just --justfile "D:\code\just\r2\justfile" @args }
function r2e { code "D:\code\just\r2\justfile" }
```

`@args` splats through untouched, so every recipe argument and every `just` flag
works as if typed in full:

```powershell
r2 --list             # enumerate every recipe, grouped
r2 sync               # full backup run
r2 av1 input.mp4      # encode -> retime -> comparison frames
r2 check-vfr in.mp4   # VFR drift verdict before committing to an encode
```

The wrapper exists because `r2` is two characters. It saves nothing else, and
either of these reaches the same recipes:

```sh
just -f just/r2/justfile --list   # -f is the short form of --justfile
cd just/r2 && just --list         # just ascends from the working directory
```

`--justfile` also reads from `JUST_JUSTFILE`, so exporting that makes a bare
`just <recipe>` work anywhere, at the cost of applying to every `just` invocation
on the machine.

## Portability

Published to be read rather than cloned. Adapting it means editing paths, not
configuration:

- Drive letters match the table above and will differ elsewhere.
- The tree is expected at `D:\code`; recipes assume it.
- `set shell` must point at a local Git Bash install.
- Runtime directories are created on demand and not tracked.

Two things look like mistakes and aren't. `@-pwsh` in `drv` uses just's `-`
prefix to ignore a non-zero exit, so stopping a cloud client that isn't running
doesn't abort the backup. And `_av1-retime` embeds a whole PowerShell script as a
single-quoted, backslash-continued block: the single quotes stop bash touching
the `$` variables, which is what makes the double parse survivable.

No credentials, tokens or keys are included. `config/rclone/` holds filter rules
only; remotes and secrets live in rclone's own config directory and are excluded
by `.gitignore`.

## Verification

[![checks](https://github.com/m-halloran/automation-toolkit/actions/workflows/checks.yml/badge.svg)](https://github.com/m-halloran/automation-toolkit/actions/workflows/checks.yml)

Every push parses the justfile with `just --summary`, which fails on a syntax
error without executing anything, and lints `python/` with ruff.

## License

MIT. See [LICENSE](LICENSE).
