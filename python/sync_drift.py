#!/usr/bin/env python3
"""Reconcile file drift between source trees and a destination copy.

When files are reorganised at the source, the destination still holds them at
their old paths, and `rclone sync` re-uploads each one and deletes the old copy
-- an extravagant way to rename a 17 GB video. This relocates them within the
destination instead, as renames inside one volume.

Nothing is deleted; an unmatched destination file is reported and left. Files
match on size and mtime within --modify-window, and content is read only to
break ties. SRC then DEST, as `rclone sync` takes them: SRC is only ever read.

-h for options, -n to preview.
"""

import argparse
import json
import os
import re
import shutil
import stat as statmod
import subprocess
import sys
from datetime import datetime

DEFAULT_LOG_DIR = "D:/code/var/logs/r2-drift"

# OS/shell metadata that must keep its exact location to keep working.
EXCLUDED_NAMES = {"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store"}

IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

WINDOWS = os.name == "nt"


# ------------------------------------------------------------------- output

class Colour:
    """ANSI codes matching the r2 justfile palette, or no-ops when disabled."""

    def __init__(self, enabled):
        codes = {
            "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
            "white": "\033[97m", "peach": "\033[38;5;214m",
            "pink": "\033[38;5;218m", "grey": "\033[90m", "reset": "\033[0m",
        }
        for name, code in codes.items():
            setattr(self, name, code if enabled else "")


C = Colour(False)

# _SILENT: --json (stdout carries the document alone) and --quiet.
# _NO_STDOUT: --json only -- --quiet still wants the verdict.
_SILENT = False
_NO_STDOUT = False


def say(msg=""):
    """Running commentary - suppressed by --quiet and --json."""
    if not _SILENT:
        print(msg, flush=True)


def out(msg=""):
    """Results that survive --quiet: the summary and any failure."""
    if not _NO_STDOUT:
        print(msg, flush=True)


def warn(msg):
    print("%s%s%s" % (C.yellow, msg, C.reset), file=sys.stderr, flush=True)


def die(msg, code=1):
    print("%s%s%s" % (C.red, msg, C.reset), file=sys.stderr, flush=True)
    sys.exit(code)


def human(n):
    """Bytes as a short human string."""
    step = 1024.0
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < step or unit == "T":
            return ("%.0f%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= step
    return "%.1fT" % n


SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([KMGTP]?)(?:i?B)?\s*$", re.I)
SIZE_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
             "T": 1024 ** 4, "P": 1024 ** 5}


def parse_size(text):
    """'100M' / '1.5G' / '4096' -> bytes."""
    m = SIZE_RE.match(str(text))
    if not m:
        raise argparse.ArgumentTypeError(
            "bad size %r - use e.g. 4096, 500K, 100M, 1.5G" % text)
    return int(float(m.group(1)) * SIZE_MULT[m.group(2).upper()])


# -------------------------------------------------------------------- paths

def norm(path):
    """Absolute path with forward slashes and no trailing separator."""
    p = os.path.abspath(os.path.expanduser(str(path))).replace("\\", "/")
    return p[:-1] if len(p) > 3 and p.endswith("/") else p


def long_path(path):
    """Prefix with \\?\\ on Windows when the path risks the 260-char limit."""
    if not WINDOWS:
        return path
    p = os.path.abspath(path)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def volume_of(path):
    """Identifier for the volume a path physically lives on.

    Symlinks resolved first: a directory under E:/ can link onto F:, and the raw
    string would report a same-volume rename that fails with WinError 17.
    """
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        probe = os.path.realpath(probe)
    except OSError:
        pass
    if WINDOWS:
        return os.path.splitdrive(probe)[0].upper()
    try:
        return str(os.stat(probe).st_dev)
    except OSError:
        return "?"


def reparse_kind(st):
    """'symlink' | 'junction' | 'reparse' | None for a *lstat* result."""
    tag = getattr(st, "st_reparse_tag", 0)
    if tag:
        if tag == IO_REPARSE_TAG_SYMLINK:
            return "symlink"
        if tag == IO_REPARSE_TAG_MOUNT_POINT:
            return "junction"
        return "reparse"
    # Non-Windows, or no reparse tag on the entry.
    if statmod.S_ISLNK(st.st_mode):
        return "symlink"
    return None


# ------------------------------------------------------------------ filters

class Filter:
    """Exclusion matcher for the rclone filter subset the filter files use.

        /pattern   anchored at root     **  spans separators
        pattern    name at any depth    *   within one segment    ?  one char

    Case-insensitive, suiting the NTFS/exFAT volumes this targets.
    """

    def __init__(self, patterns=(), files=()):
        self.file_res = []
        self.dir_res = []
        self.raw = []
        for f in files:
            self._load(f)
        for p in patterns:
            self._add(p)

    def _load(self, path):
        """Read patterns from a file, in either rclone format.

        "+ " includes are refused, not skipped: this matcher is exclusion-only,
        so dropping them while honouring the "- **" that follows would exclude
        the whole tree and report zero drift.
        """
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            die("cannot read exclude file %s: %s" % (path, exc))
        for num, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("+ "):
                die('%s:%d: "+" include rules are not supported; pass an '
                    "exclusion-only filter file: %s" % (path, num, line))
            if line.startswith("- "):
                line = line[2:].strip()
            self._add(line)

    def _add(self, pattern):
        pattern = pattern.replace("\\", "/").strip()
        if not pattern:
            return
        self.raw.append(pattern)
        self.file_res.append(self._compile(pattern))
        # "foo/**" should also prune the directory "foo" itself, so the walk
        # never descends into it.
        if pattern.endswith("/**"):
            self.dir_res.append(self._compile(pattern[:-3]))
        elif not pattern.endswith("*"):
            # A bare name may be a directory name; prune on it too.
            self.dir_res.append(self._compile(pattern))

    @staticmethod
    def _compile(pattern):
        anchored = pattern.startswith("/")
        body = pattern[1:] if anchored else pattern
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "*":
                if body[i:i + 2] == "**":
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
            elif ch == "?":
                out.append("[^/]")
            else:
                out.append(re.escape(ch))
            i += 1
        joined = "".join(out)
        prefix = "^" if anchored else "^(?:.*/)?"
        return re.compile(prefix + joined + "$", re.IGNORECASE)

    def excludes_file(self, rel):
        return any(rx.match(rel) for rx in self.file_res)

    def excludes_dir(self, rel):
        return any(rx.match(rel) for rx in self.dir_res)

    def __bool__(self):
        return bool(self.raw)


# ------------------------------------------------------------------- walking

class Entry:
    """One indexed file, positioned in the destination-relative namespace."""

    __slots__ = ("rel", "path", "size", "mtime", "origin", "real")

    def __init__(self, rel, path, size, mtime, origin, real):
        self.rel = rel        # dest-relative, forward slashes
        self.path = path
        self.size = size
        self.mtime = mtime
        self.origin = origin  # which --src produced it
        self.real = real      # realpath, for cross-source dedup

    def __repr__(self):
        return "Entry(%r, %d)" % (self.rel, self.size)


class WalkStats:
    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.excluded = 0
        self.too_small = 0
        self.links_skipped = 0
        self.errors = []


def walk_tree(root, prefix, origin, opts, stats):
    """Yield Entry objects under *root*; *prefix* maps a source into a
    subdirectory of DEST."""
    root = norm(root)
    if not os.path.isdir(root):
        die("not a directory: %s" % root)

    visited = set()
    stack = [(root, prefix.strip("/"))]

    while stack:
        abs_dir, rel_dir = stack.pop()
        stats.dirs += 1
        try:
            entries = list(os.scandir(long_path(abs_dir)))
        except OSError as exc:
            stats.errors.append("%s: %s" % (abs_dir, exc))
            continue

        for de in entries:
            rel = "%s/%s" % (rel_dir, de.name) if rel_dir else de.name

            try:
                lst = de.stat(follow_symlinks=False)
            except OSError as exc:
                stats.errors.append("%s: %s" % (rel, exc))
                continue

            kind = reparse_kind(lst)
            is_link = kind is not None

            if is_link and not opts.follow_symlinks:
                stats.links_skipped += 1
                continue

            try:
                is_dir = de.is_dir(follow_symlinks=opts.follow_symlinks)
            except OSError as exc:
                stats.errors.append("%s: %s" % (rel, exc))
                continue

            if is_dir:
                if opts.filter.excludes_dir(rel):
                    stats.excluded += 1
                    continue
                target = os.path.realpath(de.path)
                if target in visited:
                    # A link pointing back into the tree; do not loop.
                    continue
                visited.add(target)
                stack.append((de.path.replace("\\", "/"), rel))
                continue

            if de.name.lower() in EXCLUDED_NAMES:
                stats.excluded += 1
                continue
            if opts.filter.excludes_file(rel):
                stats.excluded += 1
                continue

            st = lst
            if is_link:
                try:
                    st = de.stat(follow_symlinks=True)
                except OSError as exc:
                    stats.errors.append("%s: %s" % (rel, exc))
                    continue
            if not statmod.S_ISREG(st.st_mode):
                continue
            if opts.min_size and st.st_size < opts.min_size:
                stats.too_small += 1
                continue

            stats.files += 1
            yield Entry(rel, de.path.replace("\\", "/"), st.st_size,
                        st.st_mtime, origin,
                        os.path.realpath(de.path).replace("\\", "/"))


# ------------------------------------------------------------------ identity

def key_of(entry, match_name):
    """Bucket key. Size only: mtime has a tolerance window, so hashing it would
    introduce boundary bugs."""
    if match_name:
        return (entry.size, entry.rel.rsplit("/", 1)[-1].lower())
    return (entry.size,)


def same_identity(a, b, window):
    return a.size == b.size and abs(a.mtime - b.mtime) <= window


# ------------------------------------------------------------------- hashing

_HAVE_B3SUM = None


def have_b3sum():
    global _HAVE_B3SUM
    if _HAVE_B3SUM is None:
        _HAVE_B3SUM = shutil.which("b3sum") is not None
    return _HAVE_B3SUM


def file_hash(path):
    """BLAKE3 via b3sum when present, else stdlib BLAKE2b. Returns None on
    failure, which callers treat as 'cannot disambiguate'."""
    if have_b3sum():
        try:
            res = subprocess.run(["b3sum", "--no-names", path],
                                 capture_output=True, text=True, timeout=None)
            if res.returncode == 0:
                return "b3:" + res.stdout.strip().split()[0]
            warn("b3sum failed on %s: %s" % (path, res.stderr.strip()))
        except (OSError, IndexError) as exc:
            warn("b3sum error on %s: %s" % (path, exc))
        return None
    import hashlib
    h = hashlib.blake2b(digest_size=32)
    try:
        with open(long_path(path), "rb") as fh:
            for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
                h.update(chunk)
    except OSError as exc:
        warn("hash failed on %s: %s" % (path, exc))
        return None
    return "b2:" + h.hexdigest()


class Hasher:
    """Lazy, cached, budget-aware hashing."""

    def __init__(self, max_size):
        self.cache = {}
        self.max_size = max_size
        self.bytes_read = 0
        self.count = 0

    def affordable(self, entries):
        if self.max_size is None:
            return True
        return all(e.size <= self.max_size for e in entries)

    def get(self, entry):
        if entry.path in self.cache:
            return self.cache[entry.path]
        say("%s    hashing %s (%s)%s"
            % (C.grey, entry.rel, human(entry.size), C.reset))
        digest = file_hash(entry.path)
        self.cache[entry.path] = digest
        if digest:
            self.bytes_read += entry.size
            self.count += 1
        return digest


# ------------------------------------------------------------------ planning

class Move:
    __slots__ = ("frm", "to", "rel_from", "rel_to", "size", "why")

    def __init__(self, frm, to, rel_from, rel_to, size, why):
        self.frm = frm
        self.to = to
        self.rel_from = rel_from
        self.rel_to = rel_to
        self.size = size
        self.why = why


class Plan:
    def __init__(self):
        self.moves = []
        self.duplicates = []
        self.conflicts = []
        self.missing = []      # src rels absent from dest
        self.extra = []        # dest rels with no src match
        self.changed = []      # same rel, different content


def similarity(a, b):
    """Shared leading components, plus a bonus for an identical filename."""
    pa, pb = a.split("/"), b.split("/")
    shared = 0
    for x, y in zip(pa[:-1], pb[:-1]):
        if x.lower() != y.lower():
            break
        shared += 1
    return (shared * 2) + (1 if pa[-1].lower() == pb[-1].lower() else 0)


def build_plan(src_entries, dest_entries, dest_root, opts, hasher):
    plan = Plan()
    window = opts.modify_window

    src_by_rel = {}
    for e in src_entries:
        prev = src_by_rel.get(e.rel)
        if prev is None:
            src_by_rel[e.rel] = e
        elif prev.real != e.real:
            plan.conflicts.append(
                (e.rel, "claimed by two sources (%s and %s); kept %s"
                        % (prev.origin, e.origin, prev.origin)))
    dest_by_rel = {e.rel: e for e in dest_entries}

    # Pass 1: same rel and identity -- already correct.
    settled_src, settled_dest = set(), set()
    for rel, d in dest_by_rel.items():
        s = src_by_rel.get(rel)
        if s is not None and same_identity(s, d, window):
            settled_src.add(rel)
            settled_dest.add(rel)

    orphan_dest = [d for rel, d in dest_by_rel.items() if rel not in settled_dest]
    vacant_src = [s for rel, s in src_by_rel.items() if rel not in settled_src]

    # Every source file by identity, to tell a genuine stray from a second copy
    # of a file already correctly placed.
    all_src = {}
    for s in src_by_rel.values():
        all_src.setdefault(key_of(s, opts.match_name), []).append(s)

    # Pass 2: bucket both sides by identity key.
    buckets = {}
    for s in vacant_src:
        buckets.setdefault(key_of(s, opts.match_name), ([], []))[0].append(s)
    for d in orphan_dest:
        buckets.setdefault(key_of(d, opts.match_name), ([], []))[1].append(d)

    for _key, (srcs, dests) in buckets.items():
        if not srcs:
            # No unfilled source slot wants these: either a redundant duplicate,
            # or the source has never seen the file.
            for d in sorted(dests, key=lambda e: e.rel):
                twins = [s for s in all_src.get(_key, ())
                         if s.rel in settled_src and same_identity(s, d, window)]
                if twins:
                    plan.duplicates.append(
                        (d.rel, "source has this once, already correctly at %s"
                                % twins[0].rel))
                else:
                    plan.extra.append(d.rel)
            continue
        if not dests:
            plan.missing.extend(sorted(s.rel for s in srcs))
            continue

        # Within a size bucket, mtime still has to agree within the window.
        pairs = [(s, d) for s in srcs for d in dests
                 if same_identity(s, d, window)]
        unmatched_s = [s for s in srcs if not any(p[0] is s for p in pairs)]
        unmatched_d = [d for d in dests if not any(p[1] is d for p in pairs)]
        plan.missing.extend(sorted(s.rel for s in unmatched_s))
        plan.extra.extend(sorted(d.rel for d in unmatched_d))
        if not pairs:
            continue

        cand_s = sorted({id(s): s for s, _ in pairs}.values(), key=lambda e: e.rel)
        cand_d = sorted({id(d): d for _, d in pairs}.values(), key=lambda e: e.rel)

        ambiguous = len(cand_s) > 1 or len(cand_d) > 1
        digests = None
        if ambiguous or opts.verify:
            group = cand_s + cand_d
            if not hasher.affordable(group):
                for d in cand_d:
                    plan.conflicts.append(
                        (d.rel, "%d candidate(s), and hashing is over "
                                "--max-hash-size; skipped" % len(cand_s)))
                continue
            digests = {}
            for e in group:
                digests[id(e)] = hasher.get(e)
            if any(v is None for v in digests.values()):
                for d in cand_d:
                    plan.conflicts.append((d.rel, "hashing failed; skipped"))
                continue

        _pair_group(cand_s, cand_d, digests, plan, dest_root, opts)

    # In both lists means one file differing between the sides, not
    # missing-and-extra. A sync updates it in place; nothing to relocate.
    plan.missing = sorted(set(plan.missing))
    plan.extra = sorted(set(plan.extra))
    both = set(plan.missing) & set(plan.extra)
    if both:
        plan.changed = sorted(both)
        plan.missing = [r for r in plan.missing if r not in both]
        plan.extra = [r for r in plan.extra if r not in both]
    return plan


def _pair_group(cand_s, cand_d, digests, plan, dest_root, opts):
    """Pair destination files to the source slots they belong in."""
    if digests is not None:
        groups = {}
        for s in cand_s:
            groups.setdefault(digests[id(s)], ([], []))[0].append(s)
        for d in cand_d:
            groups.setdefault(digests[id(d)], ([], []))[1].append(d)
    else:
        groups = {None: (cand_s, cand_d)}

    for _digest, (srcs, dests) in groups.items():
        if not srcs:
            plan.extra.extend(d.rel for d in dests)
            continue
        if not dests:
            plan.missing.extend(s.rel for s in srcs)
            continue

        scored = sorted(
            ((similarity(s.rel, d.rel), s, d) for s in srcs for d in dests),
            key=lambda t: (-t[0], t[1].rel, t[2].rel))

        used_s, used_d = set(), set()
        for _score, s, d in scored:
            if id(s) in used_s or id(d) in used_d:
                continue
            used_s.add(id(s))
            used_d.add(id(d))
            if s.rel == d.rel:
                continue
            target = "%s/%s" % (dest_root, s.rel)
            plan.moves.append(Move(d.path, target, d.rel, s.rel, d.size,
                                   "matches %s" % s.origin))

        leftover_d = [d for d in dests if id(d) not in used_d]
        for d in leftover_d:
            plan.duplicates.append(
                (d.rel, "extra copy; source has %d, destination has %d"
                        % (len(srcs), len(dests))))
        plan.missing.extend(s.rel for s in srcs if id(s) not in used_s)


def screen_targets(plan, dest_by_rel, settled_rels, opts):
    """Drop moves whose target is occupied by something that is not itself
    moving away."""
    leaving = {m.rel_from for m in plan.moves}
    keep = []
    for m in plan.moves:
        if m.rel_to in leaving:
            keep.append(m)          # a chain; ordering resolves it
            continue
        if m.rel_to in dest_by_rel:
            if m.rel_to in settled_rels:
                plan.duplicates.append(
                    (m.rel_from, "correct copy already at %s" % m.rel_to))
            else:
                plan.conflicts.append(
                    (m.rel_from, "target %s is occupied by a different file"
                                 % m.rel_to))
            continue
        if os.path.lexists(long_path(m.to)):
            plan.conflicts.append(
                (m.rel_from, "target %s already exists on disk" % m.rel_to))
            continue
        vol_from, vol_to = volume_of(m.frm), volume_of(m.to)
        if vol_from != vol_to and not opts.allow_cross_volume:
            plan.conflicts.append(
                (m.rel_from, "physically on %s but the target is on %s - a "
                             "move would be a %s copy, not a rename "
                             "(--allow-cross-volume to permit)"
                             % (vol_from, vol_to, human(m.size))))
            continue
        keep.append(m)
    plan.moves = keep


# ----------------------------------------------------------------- execution

def execute(plan, opts, journal_path):
    """Run the moves, deferring occupied targets. A true cycle is broken by
    staging one file through a temporary name."""
    pending = list(plan.moves)
    done = []
    staged = []
    failures = []

    while pending:
        progressed = False
        for m in list(pending):
            if os.path.lexists(long_path(m.to)):
                continue
            if _do_move(m, opts, failures):
                done.append(m)
            pending.remove(m)
            progressed = True

        if progressed or not pending:
            continue

        # Every remaining move is blocked - a rename cycle. Break it.
        m = pending.pop(0)
        tmp = "%s.drift-tmp-%d" % (m.frm, os.getpid())
        try:
            if not opts.dry_run:
                os.replace(long_path(m.frm), long_path(tmp))
            staged.append((tmp, m))
            say("%s  staged  %s%s" % (C.grey, m.rel_from, C.reset))
        except OSError as exc:
            failures.append((m.rel_from, str(exc)))

    for tmp, m in staged:
        moved = Move(tmp, m.to, m.rel_from, m.rel_to, m.size, m.why)
        if _do_move(moved, opts, failures):
            done.append(m)

    if done and not opts.dry_run:
        write_journal(journal_path, opts, done)
    return done, failures


def _do_move(m, opts, failures):
    label = "%s%s  ->  %s%s" % (C.white, m.rel_from, m.rel_to, C.reset)
    if opts.dry_run:
        say("  would move  " + label)
        return True
    try:
        parent = os.path.dirname(m.to)
        if parent:
            os.makedirs(long_path(parent), exist_ok=True)
        try:
            os.replace(long_path(m.frm), long_path(m.to))
        except OSError as exc:
            # EXDEV / WinError 17: paths straddle a device boundary. screen_targets()
            # rejects these unless opted in, so reaching here means they did.
            if not _is_cross_device(exc):
                raise
            if not opts.allow_cross_volume:
                raise
            say("%s  crossing volumes, copying %s%s"
                % (C.grey, human(m.size), C.reset))
            shutil.move(long_path(m.frm), long_path(m.to))
    except OSError as exc:
        failures.append((m.rel_from, str(exc)))
        out("%s  FAILED  %s: %s%s" % (C.red, m.rel_from, exc, C.reset))
        return False
    say("  moved  " + label)
    return True


def _is_cross_device(exc):
    import errno
    return getattr(exc, "errno", None) == errno.EXDEV or \
        getattr(exc, "winerror", None) == 17


def prune_empty_dirs(root, opts):
    """Remove directories left empty by the moves. Never touches the root."""
    removed = 0
    root = norm(root)
    for cur, dirnames, filenames in os.walk(long_path(root), topdown=False):
        cur_n = cur.replace("\\", "/")
        if norm(cur_n) == root:
            continue
        if filenames or dirnames:
            continue
        try:
            if not opts.dry_run:
                os.rmdir(long_path(cur))
            removed += 1
            say("%s  pruned  %s%s"
                % (C.grey, os.path.relpath(cur_n, root).replace("\\", "/"),
                   C.reset))
        except OSError:
            pass
    return removed


# ------------------------------------------------------------------- journal

def journal_dir(opts):
    d = norm(opts.log_dir)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        die("cannot create log directory %s: %s" % (d, exc))
    return d


def write_journal(path, opts, moves):
    payload = {
        "version": 1,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "dest": opts.dest,
        "sources": [s for s, _ in opts.sources],
        "moves": [{"from": m.to, "to": m.frm,
                   "rel_from": m.rel_to, "rel_to": m.rel_from,
                   "size": m.size} for m in moves],
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        say("%s  journal: %s%s" % (C.grey, path, C.reset))
    except OSError as exc:
        warn("could not write journal %s: %s" % (path, exc))


def list_journals(opts):
    d = journal_dir(opts)
    files = sorted(f for f in os.listdir(d)
                   if f.startswith("drift_") and f.endswith(".json"))
    if not files:
        say("No journals in %s" % d)
        return 0
    say("%sJournals in %s%s" % (C.white, d, C.reset))
    for name in files:
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                data = json.load(fh)
            say("  %-34s %s  %d move(s)  ->  %s"
                % (name, data.get("timestamp", "?"),
                   len(data.get("moves", [])), data.get("dest", "?")))
        except (OSError, ValueError):
            say("  %-34s (unreadable)" % name)
    return 0


def undo(opts):
    d = journal_dir(opts)
    if opts.undo is True:
        files = sorted(f for f in os.listdir(d)
                       if f.startswith("drift_") and f.endswith(".json"))
        if not files:
            die("no journal to undo in %s" % d)
        target = os.path.join(d, files[-1])
    else:
        target = opts.undo if os.path.isabs(opts.undo) \
            else os.path.join(d, opts.undo)

    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        die("cannot read journal %s: %s" % (target, exc))

    moves = data.get("moves", [])
    say("%sUndo %s  (%d move(s), recorded %s)%s"
        % (C.white, os.path.basename(target), len(moves),
           data.get("timestamp", "?"), C.reset))
    say()

    ok = failed = skipped = 0
    for rec in moves:
        frm, to = rec["from"], rec["to"]
        if not os.path.lexists(long_path(frm)):
            say("%s  gone, skipped  %s%s" % (C.yellow, rec["rel_from"], C.reset))
            skipped += 1
            continue
        if os.path.lexists(long_path(to)):
            say("%s  occupied, skipped  %s%s" % (C.yellow, rec["rel_to"], C.reset))
            skipped += 1
            continue
        if opts.dry_run:
            say("  would restore  %s%s  ->  %s%s"
                % (C.white, rec["rel_from"], rec["rel_to"], C.reset))
            ok += 1
            continue
        try:
            os.makedirs(long_path(os.path.dirname(to)), exist_ok=True)
            os.replace(long_path(frm), long_path(to))
            say("  restored  %s%s  ->  %s%s"
                % (C.white, rec["rel_from"], rec["rel_to"], C.reset))
            ok += 1
        except OSError as exc:
            say("%s  FAILED  %s: %s%s" % (C.red, rec["rel_to"], exc, C.reset))
            failed += 1

    say()
    say("%s%d restored, %d skipped, %d failed%s"
        % (C.green if not failed else C.yellow, ok, skipped, failed, C.reset))
    if not failed and not opts.dry_run:
        try:
            os.rename(target, target + ".done")
        except OSError:
            pass
    return 0


# -------------------------------------------------------------------- report

def section(title, rows, empty_note, colour, cap):
    say()
    say("%s--- %s: %d ---%s" % (colour, title, len(rows), C.reset))
    if not rows:
        say("%s    %s%s" % (C.grey, empty_note, C.reset))
        return
    for row in rows[:cap]:
        say("    %s" % row)
    if len(rows) > cap:
        say("%s    ... %d more (re-run with --full)%s"
            % (C.grey, len(rows) - cap, C.reset))


def report(plan, opts, src_stats, dest_stats, hasher):
    cap = 10 ** 9 if opts.full else 20

    say()
    say("%sIndexed%s  %d source file(s) in %d dir(s), %d destination file(s) "
        "in %d dir(s)" % (C.white, C.reset, src_stats.files, src_stats.dirs,
                          dest_stats.files, dest_stats.dirs))
    notes = []
    if src_stats.excluded or dest_stats.excluded:
        notes.append("%d excluded" % (src_stats.excluded + dest_stats.excluded))
    if src_stats.too_small or dest_stats.too_small:
        notes.append("%d under --min-size"
                     % (src_stats.too_small + dest_stats.too_small))
    if src_stats.links_skipped or dest_stats.links_skipped:
        notes.append("%d link(s) not followed"
                     % (src_stats.links_skipped + dest_stats.links_skipped))
    if hasher.count:
        notes.append("%d file(s) hashed, %s read"
                     % (hasher.count, human(hasher.bytes_read)))
    if notes:
        say("%s         %s%s" % (C.grey, ", ".join(notes), C.reset))

    errors = src_stats.errors + dest_stats.errors
    section("ERRORS", errors, "none", C.red, cap)
    section("CONFLICTS - skipped, resolve by hand",
            ["%s%s%s  (%s)" % (C.white, rel, C.reset, note)
             for rel, note in plan.conflicts],
            "none", C.yellow, cap)
    section("DUPLICATES - extra copies on destination, left alone",
            ["%s%s%s  (%s)" % (C.white, rel, C.reset, note)
             for rel, note in plan.duplicates],
            "none", C.yellow, cap)

    section("DIFFERENT CONTENT AT THE SAME PATH - a sync would update these",
            plan.changed, "none", C.yellow, cap)

    if opts.show_diff:
        section("ON SOURCE, NOT ON DESTINATION - a sync would copy these",
                plan.missing, "none - destination has every source file",
                C.grey, cap)
        section("ON DESTINATION, NOT ON SOURCE - a sync may delete these",
                plan.extra, "none - destination has no strays", C.grey, cap)


def summarise(plan, done, failures, pruned, opts):
    total = sum(m.size for m in (plan.moves if opts.dry_run else done))
    out()
    if not plan.moves:
        out("%sNo files need relocating - %s layout matches %s%s"
            % (C.green, opts.dest, opts.sources[0][0], C.reset))
    elif opts.dry_run:
        out("%s%d file(s) would move inside %s, %s relocated in place%s"
            % (C.peach, len(plan.moves), opts.dest, human(total), C.reset))
        out("%sDry run - nothing was changed. Re-run without -n to apply.%s"
            % (C.grey, C.reset))
    else:
        out("%s%d file(s) moved inside %s, %s relocated in place%s"
            % (C.green, len(done), opts.dest, human(total), C.reset))
        out("%s%s of transfer avoided on the next sync%s"
            % (C.green, human(total), C.reset))
    if pruned:
        out("%s%d empty director%s %s%s"
            % (C.grey, pruned, "y" if pruned == 1 else "ies",
               "would be pruned" if opts.dry_run else "pruned", C.reset))
    # "Nothing to relocate" is not "the trees agree" -- surface the rest so the
    # verdict cannot mislead when detail is capped or behind --show-diff.
    rest = []
    if plan.duplicates:
        rest.append("%d duplicate(s) on destination" % len(plan.duplicates))
    if plan.changed:
        rest.append("%d differing at the same path" % len(plan.changed))
    if plan.missing:
        rest.append("%d only on source" % len(plan.missing))
    if plan.extra:
        rest.append("%d only on destination" % len(plan.extra))
    if rest:
        hint = "" if opts.show_diff else "  (--show-diff to list)"
        out("%s%s%s%s" % (C.yellow, ", ".join(rest), hint, C.reset))

    if failures:
        out("%s%d move(s) failed%s" % (C.red, len(failures), C.reset))
    if plan.conflicts:
        out("%s%d conflict(s) need attention%s"
            % (C.yellow, len(plan.conflicts), C.reset))


# ----------------------------------------------------------------------- cli

def parse_source(text):
    """'PATH' or 'PATH=DEST_SUBPATH' -> (path, subpath)."""
    raw = str(text)
    # Split on the last '=' so drive letters and '=' in names survive.
    if "=" in raw:
        path, sub = raw.rsplit("=", 1)
        if path and not sub.strip("/ "):
            return norm(path), ""
        return norm(path), sub.strip().replace("\\", "/").strip("/")
    return norm(raw), ""


def build_parser():
    p = argparse.ArgumentParser(
        prog="sync_drift.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Relocate files inside DEST so its layout matches the source "
            "tree(s).\n"
            "Files are identified by size and modification time; content is "
            "only\nread when an identity is ambiguous. Nothing is ever "
            "deleted."),
        epilog=__doc__[__doc__.index("Usage:"):])

    # SRC before DEST, matching `rclone sync`. SRC is read-only; DEST is the
    # tree that gets rewritten.
    p.add_argument("src", nargs="?",
                   help="source tree whose layout is authoritative "
                        "(only ever read)")
    p.add_argument("dest", nargs="?",
                   help="destination tree to reorganise (never deleted from)")
    p.add_argument("-s", "--src", dest="extra_src", action="append",
                   default=[], metavar="PATH",
                   help="additional source tree, unioned into the same "
                        "namespace; repeatable. 'PATH=SUBPATH' places it at "
                        "SUBPATH inside DEST. Earlier sources win on overlap.")

    g = p.add_argument_group("matching")
    g.add_argument("--match-name", action="store_true",
                   help="also require equal filenames, not just size+mtime")
    g.add_argument("--modify-window", type=float, default=2.0, metavar="SEC",
                   help="mtime tolerance in seconds (default: 2, matching "
                        "rclone and FAT/exFAT granularity)")
    g.add_argument("--min-size", type=parse_size, default=0, metavar="SIZE",
                   help="ignore files smaller than this, e.g. 100M, 1.5G")
    g.add_argument("--verify", action="store_true",
                   help="hash every candidate, not just ambiguous ones. Slow: "
                        "reads both copies of every matched file.")
    g.add_argument("--max-hash-size", type=parse_size, default=None,
                   metavar="SIZE",
                   help="refuse to hash files above this size; report them as "
                        "conflicts instead")

    g = p.add_argument_group("traversal")
    g.add_argument("-l", "--links", dest="follow_symlinks",
                   action="store_true",
                   help="descend into symlinks and junctions. Note that files "
                        "reached this way may sit on another volume, so "
                        "relocating them is a copy, not a rename.")
    g.add_argument("-x", "--exclude", action="append", default=[],
                   metavar="PATTERN",
                   help="exclude paths matching an rclone-style pattern; "
                        "repeatable")
    g.add_argument("--exclude-from", action="append", default=[],
                   metavar="FILE",
                   help="read exclude patterns from a file, one per line; "
                        "repeatable")

    g = p.add_argument_group("actions")
    g.add_argument("-n", "--dry-run", action="store_true",
                   help="show the plan without touching anything")
    g.add_argument("--prune-empty-dirs", action="store_true",
                   help="remove directories left empty by the moves")
    g.add_argument("--allow-cross-volume", action="store_true",
                   help="permit moves between volumes. These are copies, not "
                        "renames, and cost full read+write.")

    g = p.add_argument_group("output")
    g.add_argument("--show-diff", action="store_true",
                   help="also list files present on only one side")
    g.add_argument("--full", action="store_true",
                   help="print every row instead of capping sections at 20")
    g.add_argument("--json", action="store_true",
                   help="emit the plan as JSON on stdout and nothing else")
    g.add_argument("-q", "--quiet", action="store_true",
                   help="mute the running commentary; print only the closing "
                        "summary and any failures")
    g.add_argument("--silent", action="store_true",
                   help="print literally nothing - no summary, no commentary")
    g.add_argument("--no-color", action="store_true", help="disable colour")
    g.add_argument("--strict", action="store_true",
                   help="exit 2 if any conflicts were reported")

    g = p.add_argument_group("journal")
    g.add_argument("--log-dir", default=DEFAULT_LOG_DIR, metavar="DIR",
                   help="where run journals are written (default: %s)"
                        % DEFAULT_LOG_DIR)
    g.add_argument("--undo", nargs="?", const=True, metavar="JOURNAL",
                   help="revert a run; defaults to the most recent journal")
    g.add_argument("--list-undo", action="store_true",
                   help="list available journals")
    return p


def emit_json(plan, done, opts):
    payload = {
        "dest": opts.dest,
        "sources": [{"path": s, "at": sub} for s, sub in opts.sources],
        "dry_run": opts.dry_run,
        "moves": [{"from": m.rel_from, "to": m.rel_to, "size": m.size}
                  for m in plan.moves],
        # Nothing is applied during a dry run, however many moves were planned.
        "applied": ([] if opts.dry_run
                    else [{"from": m.rel_from, "to": m.rel_to} for m in done]),
        "conflicts": [{"path": r, "note": n} for r, n in plan.conflicts],
        "duplicates": [{"path": r, "note": n} for r, n in plan.duplicates],
        "missing_on_dest": plan.missing,
        "extra_on_dest": plan.extra,
        "changed_in_place": plan.changed,
    }
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def main(argv=None):
    global C, _SILENT, _NO_STDOUT

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = build_parser()
    opts = parser.parse_args(argv)
    _NO_STDOUT = bool(opts.json or opts.silent)
    _SILENT = bool(opts.json or opts.quiet or opts.silent)
    colour_ok = (not opts.no_color and not opts.json
                 and os.environ.get("NO_COLOR") is None
                 and hasattr(sys.stdout, "isatty") and sys.stdout.isatty())
    C = Colour(colour_ok)

    if opts.list_undo:
        return list_journals(opts)
    if opts.undo:
        return undo(opts)

    if not opts.src or not opts.dest:
        parser.error("both SRC and DEST are required, in that order "
                     "(or use --undo / --list-undo)")

    opts.dest = norm(opts.dest)
    if not os.path.isdir(opts.dest):
        die("destination is not a directory: %s" % opts.dest)
    opts.sources = [parse_source(opts.src)] + \
        [parse_source(s) for s in opts.extra_src]
    opts.filter = Filter(opts.exclude, opts.exclude_from)

    for path, _sub in opts.sources:
        if not os.path.isdir(path):
            die("source is not a directory: %s" % path)
        if norm(path) == opts.dest:
            die("source and destination are the same path: %s" % path)

    say()
    say("%sReconciling drift%s" % (C.white, C.reset))
    for path, sub in opts.sources:
        say("  read    %s%s" % (path, ("  ->  %s/" % sub) if sub else ""))
    say("  %srewrite %s%s" % (C.peach, opts.dest, C.reset))
    if opts.verify and not have_b3sum():
        warn("b3sum not found on PATH - falling back to BLAKE2b")

    src_stats, dest_stats = WalkStats(), WalkStats()
    src_entries = []
    seen_rel = {}
    for path, sub in opts.sources:
        for e in walk_tree(path, sub, path, opts, src_stats):
            prev = seen_rel.get(e.rel)
            if prev is not None and prev.real == e.real:
                continue  # same physical file reached two ways
            seen_rel[e.rel] = e
            src_entries.append(e)

    dest_entries = list(walk_tree(opts.dest, "", opts.dest, opts, dest_stats))
    dest_by_rel = {e.rel: e for e in dest_entries}

    hasher = Hasher(opts.max_hash_size)
    plan = build_plan(src_entries, dest_entries, opts.dest, opts, hasher)

    src_rels = {e.rel for e in src_entries}
    settled = {rel for rel in dest_by_rel
               if rel in src_rels
               and same_identity(seen_rel[rel], dest_by_rel[rel],
                                 opts.modify_window)}
    screen_targets(plan, dest_by_rel, settled, opts)

    report(plan, opts, src_stats, dest_stats, hasher)
    say()
    say("%s--- MOVES: %d ---%s" % (C.peach, len(plan.moves), C.reset))
    if not plan.moves:
        say("%s    destination layout already matches the source%s"
            % (C.grey, C.reset))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = os.path.join(journal_dir(opts), "drift_%s.json" % stamp) \
        if plan.moves and not opts.dry_run else None

    done, failures = execute(plan, opts, jpath)

    pruned = 0
    if opts.prune_empty_dirs and (done or opts.dry_run):
        pruned = prune_empty_dirs(opts.dest, opts)

    if opts.json:
        emit_json(plan, done, opts)
    else:
        summarise(plan, done, failures, pruned, opts)
        out()

    if failures:
        return 1
    if opts.strict and plan.conflicts:
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
