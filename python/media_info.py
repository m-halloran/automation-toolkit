#!/usr/bin/env python3

"""List media files in a directory and report codec info.

Reads each file with ffprobe, prints a table, and writes a plain-text log.
Per file: container, duration, bitrate, video and audio stream detail including
a VFR flag, and a count of any other streams.

Requires ffprobe on PATH. -h for options.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime

LOG_ROOT = r"D:\code\var\logs"
# 0 = keep all.
MAX_LOG_FILES = 10

MEDIA_EXTS = {
    # video
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".vob", ".ogv", ".3gp",
    # audio
    ".mp3", ".aac", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".wma",
    ".alac", ".aiff", ".ape",
}


def find_ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        sys.exit(
            "ERROR: ffprobe not found on PATH.\n"
            "Install ffmpeg: https://ffmpeg.org/download.html\n"
            "  Windows: winget install Gyan.FFmpeg"
        )
    return exe


def gather_files(root: str, recursive: bool) -> list[str]:
    files = []
    if recursive:
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                    files.append(os.path.join(dirpath, name))
    else:
        for name in os.listdir(root):
            full = os.path.join(root, name)
            if os.path.isfile(full) and os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                files.append(full)
    return sorted(files)


def run_ffprobe(ffprobe: str, path: str) -> dict | None:
    cmd = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0 or not (out.stdout or "").strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def frac_to_float(value: str | None) -> float | None:
    """Parse '24000/1001' style fractions into a float."""
    if not value:
        return None
    try:
        if "/" in value:
            num, den = value.split("/")
            den = float(den)
            return float(num) / den if den else None
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


def detect_vfr_accurate(ffprobe: str, path: str, stream_index: int) -> bool | None:
    """Scan packet timestamps to decide if frame durations vary. Slower."""
    cmd = [
        ffprobe, "-v", "quiet", "-select_streams", f"v:{stream_index}",
        "-show_entries", "packet=pts_time", "-print_format", "json", path,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        packets = json.loads(out.stdout).get("packets", [])
    except (json.JSONDecodeError, AttributeError):
        return None
    times = sorted(
        float(p["pts_time"]) for p in packets if p.get("pts_time") not in (None, "N/A")
    )
    if len(times) < 3:
        return None
    deltas = [round(times[i + 1] - times[i], 4) for i in range(len(times) - 1)]
    # ignore the last delta which is often irregular; count distinct durations
    distinct = {d for d in deltas[:-1] if d > 0}
    return len(distinct) > 1


def human_bitrate(bps: str | int | None) -> str:
    if bps in (None, "", "N/A"):
        return "-"
    try:
        kb = int(bps) / 1000
    except (ValueError, TypeError):
        return "-"
    if kb >= 1000:
        return f"{kb / 1000:.2f} Mb/s"
    return f"{kb:.0f} kb/s"


def human_duration(seconds: str | float | None) -> str:
    if seconds in (None, "", "N/A"):
        return "-"
    try:
        s = float(seconds)
    except (ValueError, TypeError):
        return "-"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _human_size(size: str | int | None) -> str:
    if size in (None, "", "N/A"):
        return "-"
    try:
        b = float(size)
    except (ValueError, TypeError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def summarize(data: dict, ffprobe: str, path: str, accurate: bool) -> dict:
    fmt = data.get("format", {})
    streams = data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    n_sub = sum(1 for s in streams if s.get("codec_type") == "subtitle")
    n_other = sum(
        1 for s in streams
        if s.get("codec_type") not in ("video", "audio", "subtitle")
    )

    row = {
        "file": os.path.basename(path),
        "path": path,
        "container": fmt.get("format_name", "-"),
        "duration": human_duration(fmt.get("duration")),
        "size": _human_size(fmt.get("size")),
        "overall_bitrate": human_bitrate(fmt.get("bit_rate")),
        "v_codec": "-", "v_profile": "-", "v_pixfmt": "-",
        "v_res": "-", "v_fps": "-", "v_vfr": "-", "v_bitrate": "-",
        "a_codec": "-", "a_profile": "-", "a_rate": "-",
        "a_channels": "-", "a_bitrate": "-",
        "subs": n_sub, "other": n_other,
    }

    if video:
        row["v_codec"] = video.get("codec_name", "-")
        row["v_profile"] = video.get("profile", "-") or "-"
        row["v_pixfmt"] = video.get("pix_fmt", "-") or "-"
        w, h = video.get("width"), video.get("height")
        row["v_res"] = f"{w}x{h}" if w and h else "-"
        row["v_bitrate"] = human_bitrate(video.get("bit_rate"))

        r_fps = frac_to_float(video.get("r_frame_rate"))
        avg_fps = frac_to_float(video.get("avg_frame_rate"))
        if avg_fps:
            row["v_fps"] = f"{avg_fps:.2f}".rstrip("0").rstrip(".")
        elif r_fps:
            row["v_fps"] = f"{r_fps:.2f}".rstrip("0").rstrip(".")

        # VFR detection
        if accurate:
            vfr = detect_vfr_accurate(ffprobe, path, 0)
            row["v_vfr"] = "?" if vfr is None else ("VFR" if vfr else "CFR")
        else:
            # quick heuristic: nominal vs average frame rate diverge => likely VFR
            if r_fps and avg_fps:
                diverge = abs(r_fps - avg_fps) / max(r_fps, avg_fps) > 0.01
                row["v_vfr"] = "VFR?" if diverge else "CFR"
            else:
                row["v_vfr"] = "?"

    if audio:
        row["a_codec"] = audio.get("codec_name", "-")
        row["a_profile"] = audio.get("profile", "-") or "-"
        sr = audio.get("sample_rate")
        row["a_rate"] = f"{int(sr) // 1000} kHz" if sr and str(sr).isdigit() else "-"
        ch = audio.get("channels")
        layout = audio.get("channel_layout")
        row["a_channels"] = layout or (str(ch) if ch else "-")
        row["a_bitrate"] = human_bitrate(audio.get("bit_rate"))

    return row


def char_width(ch: str) -> int:
    """Terminal cell width of a single character (handles CJK / emoji)."""
    o = ord(ch)
    if o in (0xFE0E, 0xFE0F):  # variation selectors
        return 0
    if o in (0x200B, 0x200C, 0x200D, 0xFEFF):  # zero-width joiners/space
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):  # wide / fullwidth
        return 2
    # True emoji pictographs render double-width. Plain text-style symbols
    # (e.g. hearts, card suits) stay single-width unless a following U+FE0F
    # forces emoji presentation -- that pairing is handled in _units().
    if 0x1F000 <= o <= 0x1FAFF:
        return 2
    return 1


def _units(s: str) -> list[tuple[str, int]]:
    """Split into display units, pairing a base char with a trailing U+FE0F
    (emoji variation selector) so the pair counts as one double-width cell."""
    units, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if i + 1 < n and s[i + 1] == "️":
            units.append((ch + s[i + 1], 2))  # emoji presentation
            i += 2
        else:
            units.append((ch, char_width(ch)))
            i += 1
    return units


def disp_width(s: str) -> int:
    """Total terminal display width of a string."""
    return sum(w for _, w in _units(str(s)))


def _take_width(s: str, budget: int, from_end: bool = False) -> str:
    """Take display units from one end until `budget` columns are used."""
    units = _units(s)
    if from_end:
        units = list(reversed(units))
    out, total = [], 0
    for text, w in units:
        if total + w > budget:
            break
        out.append(text)
        total += w
    if from_end:
        out.reverse()
    return "".join(out)


def pad(s: str, width: int) -> str:
    """Left-justify to a target display width (not code-point count)."""
    s = str(s)
    return s + " " * max(0, width - disp_width(s))


def truncate_name(name: str, maxlen: int) -> str:
    """Shorten a filename to `maxlen` display columns with a middle ellipsis,
    preserving the extension. Width-aware so CJK/emoji names line up."""
    if maxlen <= 0 or disp_width(name) <= maxlen:
        return name
    root, ext = os.path.splitext(name)
    budget = maxlen - disp_width(ext) - 1  # room for the ellipsis (1 col)
    if budget < 4:
        return _take_width(name, maxlen - 1) + "…"
    head_budget = (budget + 1) // 2
    tail_budget = budget - head_budget
    head = _take_width(root, head_budget)
    tail = _take_width(root, tail_budget, from_end=True)
    return f"{head}…{tail}{ext}"


def render_table(rows: list[dict], name_width: int = 40) -> str:
    """Compact fixed-width table covering the key fields."""
    # Truncate long filenames just for display; the log DETAILS keeps full names.
    rows = [dict(r, file=truncate_name(r["file"], name_width)) for r in rows]
    headers = [
        ("file", "File"),
        ("v_codec", "Video"),
        ("v_res", "Resolution"),
        ("v_fps", "FPS"),
        ("v_vfr", "VFR"),
        ("v_bitrate", "V.Bitrate"),
        ("a_codec", "Audio"),
        ("a_channels", "Ch"),
        ("a_bitrate", "A.Bitrate"),
        ("duration", "Dur"),
        ("size", "Size"),
    ]
    widths = {}
    for key, label in headers:
        widths[key] = max(len(label), *(disp_width(r[key]) for r in rows)) if rows else len(label)

    def fmt_row(values):
        return "  ".join(pad(v, widths[key]) for (key, _), v in zip(headers, values))

    lines = [fmt_row([label for _, label in headers])]
    lines.append("  ".join("-" * widths[key] for key, _ in headers))
    for r in rows:
        lines.append(fmt_row([r[key] for key, _ in headers]))
    return "\n".join(lines)


def render_detail(rows: list[dict]) -> str:
    """Verbose per-file block for the log file."""
    out = []
    for r in rows:
        out.append(f"=== {r['file']} ===")
        out.append(f"  Path       : {r['path']}")
        out.append(f"  Container  : {r['container']}")
        out.append(f"  Duration   : {r['duration']}    Size: {r['size']}    Overall: {r['overall_bitrate']}")
        out.append(f"  Video      : {r['v_codec']} ({r['v_profile']}), {r['v_pixfmt']}, "
                    f"{r['v_res']}, {r['v_fps']} fps [{r['v_vfr']}], {r['v_bitrate']}")
        out.append(f"  Audio      : {r['a_codec']} ({r['a_profile']}), {r['a_rate']}, "
                    f"{r['a_channels']}, {r['a_bitrate']}")
        out.append(f"  Subtitles  : {r['subs']}    Other streams: {r['other']}")
        out.append("")
    return "\n".join(out)


def prune_logs(log_dir: str, keep: int) -> list[str]:
    """Delete all but the `keep` most-recent media_info_*.txt logs in log_dir.
    Returns the list of removed file paths."""
    if keep <= 0 or not os.path.isdir(log_dir):
        return []
    logs = [
        os.path.join(log_dir, f)
        for f in os.listdir(log_dir)
        if f.lower().endswith(".txt") and os.path.isfile(os.path.join(log_dir, f))
    ]
    logs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    removed = []
    for old in logs[keep:]:
        try:
            os.remove(old)
            removed.append(old)
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser(
        description="List media files in a directory with codec/bitrate/fps info."
    )
    ap.add_argument("directory", nargs="?", default=".",
                    help="Directory to scan (default: current directory)")
    ap.add_argument("-r", "--recursive", action="store_true",
                    help="Scan subdirectories too")
    ap.add_argument("--accurate", action="store_true",
                    help="Confirm VFR by scanning frame timestamps (slower but definitive)")
    ap.add_argument("--name-width", type=int, default=57,
                    help="Max filename width in the terminal table before truncating (default: 57, 0 = no limit)")
    ap.add_argument("--log", default=None,
                    help=f"Full log file path (default: {LOG_ROOT}\\r2-media-info\\<foldername>_<timestamp>.txt)")
    args = ap.parse_args()

    # Windows consoles default to cp1252; make output UTF-8 safe.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    directory = os.path.abspath(args.directory)
    if not os.path.isdir(directory):
        sys.exit(f"ERROR: not a directory: {directory}")

    ffprobe = find_ffprobe()
    files = gather_files(directory, args.recursive)
    if not files:
        print(f"No media files found in {directory}")
        return

    rows = []
    for i, path in enumerate(files, 1):
        print(f"\rProbing {i}/{len(files)}...", end="", file=sys.stderr, flush=True)
        data = run_ffprobe(ffprobe, path)
        if data is None:
            try:
                sz = os.path.getsize(path)
            except OSError:
                sz = None
            rows.append({
                "file": os.path.basename(path), "path": path,
                "container": "UNREADABLE", "duration": "-", "size": _human_size(sz),
                "overall_bitrate": "-",
                "v_codec": "-", "v_profile": "-", "v_pixfmt": "-", "v_res": "-",
                "v_fps": "-", "v_vfr": "-", "v_bitrate": "-",
                "a_codec": "-", "a_profile": "-", "a_rate": "-",
                "a_channels": "-", "a_bitrate": "-", "subs": 0, "other": 0,
            })
            continue
        rows.append(summarize(data, ffprobe, path, args.accurate))
    print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    table = render_table(rows, args.name_width)
    detail = render_detail(rows)

    print(table)

    # Build log path: LOG_ROOT\<scanned-folder-name>\media_info_<timestamp>.txt
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.log:
        log_path = args.log
    else:
        folder_name = os.path.basename(directory.rstrip("\\/")) or "root"
        log_dir = os.path.join(LOG_ROOT, "r2-media-info")
        log_path = os.path.join(
            log_dir, f"{folder_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    vfr_mode = "accurate (frame scan)" if args.accurate else "fast (heuristic)"
    header = (
        f"Media codec report\n"
        f"Directory : {directory}\n"
        f"Generated : {stamp}\n"
        f"Files     : {len(rows)}\n"
        f"VFR mode  : {vfr_mode}\n"
        f"{'=' * 60}\n\n"
    )
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("SUMMARY TABLE\n")
        fh.write(table + "\n\n")
        fh.write("=" * 60 + "\n\n")
        fh.write("DETAILS\n\n")
        fh.write(detail)

    print(f"\nLog written to: {log_path}", file=sys.stderr)

    # Retention: keep only the most-recent logs (skip when a custom --log is used).
    if not args.log:
        removed = prune_logs(os.path.dirname(log_path), MAX_LOG_FILES)
        if removed:
            print(f"Removed {len(removed)} old log(s), keeping newest {MAX_LOG_FILES}.",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
