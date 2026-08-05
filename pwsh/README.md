# pwsh: PowerShell utilities

Heavier logic lives here rather than in a recipe body. A recipe running under
`bash` that shells out to `pwsh -Command` is parsed twice, once by each shell, so
the quoting becomes the bug surface. A `.ps1` called with `-NoProfile -File`
avoids the double parse entirely.

| Script | Reached by | What it does |
| --- | --- | --- |
| `Measure-Av1Crf.ps1` | `r2 av1-crf` | Sample-encodes short clips from spread positions, avoiding intros and credits, and finds the CRF each needs to hit a VMAF target. Recommends the most conservative. Then re-encodes each clip at fixed CRF, exactly as the real encode runs, to measure throughput and project a completion time. Appends every run to a CSV. |
| `Get-Index.ps1` | `r2 index` | Recursive directory index, written to a numbered snapshot per run so state is recorded over time rather than overwritten. Broken links and unreadable paths are labelled instead of aborting the walk. |
| `Get-SystemSpecs.ps1` | `r2 system-info`, `r2 drive-info` | CPU/RAM/GPU/OS plus logical and physical disk detail with live I/O counters. Used to size encoder worker counts and spot per-drive bottlenecks. |

## House style

- `param()` first, typed, with defaults matching the recipe's own.
- Exit non-zero on failure. Recipes run under `bash -cu`, which does **not** set
  `errexit`, so a caller that cares must test the exit code.
- Spelled-out parameter names at call sites (`-InputFile`, not `-i`).
