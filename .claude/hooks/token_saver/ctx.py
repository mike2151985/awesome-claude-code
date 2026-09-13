"""`ctx` — the cheap half of every expensive command.

Each sub-command answers exactly the question the expensive form answers, then
prints a footer saying what was left out and the one command that retrieves it.
No sub-command ever invents, summarises or paraphrases content: it selects.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import compress, config, core

MAX_CAPTURE_BYTES = 8_000_000


# --- helpers ------------------------------------------------------------------


def _emit(lines: Sequence[str], *, kind: str, saved: int = 0) -> int:
    text = "\n".join(lines)
    sys.stdout.write(text + "\n")
    core.record(kind, saved_tokens=saved, spent_tokens=core.est_tokens(text))
    core.touch_activity()
    return 0


def _resolve(target: str) -> Path:
    path = Path(target)
    return path if path.is_absolute() else core.project_dir() / path


def _cache_file(prefix: str, suffix: str = ".log") -> Path:
    return core.state_dir() / "logs" / f"{prefix}-{int(time.time())}-{os.getpid()}{suffix}"


def _is_binary(path: Path) -> bool:
    try:
        return b"\0" in path.open("rb").read(2048)
    except OSError:
        return False


# --- read ---------------------------------------------------------------------


def cmd_read(args: list[str]) -> int:
    """read FILE [START [END]] — a whole small file, or shape + window of a big one."""
    if not args:
        print("usage: ctx read FILE [START [END]]", file=sys.stderr)
        return 2
    path = _resolve(args[0])
    if not path.is_file():
        print(f"ctx read: no such file: {path}", file=sys.stderr)
        return 1
    if _is_binary(path):
        size = path.stat().st_size
        return _emit([f"{path}: binary file, {size} bytes — not dumped."], kind="ctx.read")

    start = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    end = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
    raw = path.read_text(encoding="utf-8", errors="replace").split("\n")
    if raw and raw[-1] == "":  # a trailing newline is not a line
        raw.pop()
    total = len(raw)
    full_tokens = core.est_tokens("\n".join(raw))

    if start:
        end = end or min(total, start + config.BIG_FILE_LINES)
        window = raw[start - 1:end]
        body = [
            f"{path} [lines {start}-{min(end, total)} of {total}]",
            *compress.numbered(window, start),
            (f"— window shown; continue with: ctx read {path} {end + 1} {min(total, end + config.BIG_FILE_LINES)}"
             if end < total else f"— end of file ({total} lines)."),
        ]
        return _emit(body, kind="ctx.read", saved=max(0, full_tokens - core.est_tokens("\n".join(body))))

    if total <= config.BIG_FILE_LINES and path.stat().st_size <= config.BIG_FILE_BYTES:
        return _emit([f"{path} [{total} lines, complete]", *compress.numbered(raw)], kind="ctx.read")

    shape = compress.outline(path)
    head = raw[:config.HEAD_LINES]
    body = [
        f"{path} [{total} lines, {path.stat().st_size} bytes — shape + head shown, nothing deleted]",
        "",
        "## outline",
        *(shape or ["  (no recognisable definitions)"]),
        "",
        f"## first {len(head)} lines",
        *compress.numbered(head),
        "",
        f"— {total - len(head)} lines not shown. Fetch any range with:",
        f"    ctx read {path} <START> <END>        # e.g. ctx read {path} {config.HEAD_LINES + 1} {config.HEAD_LINES + 200}",
        f"    ctx grep '<pattern>' {path}          # jump straight to what you need",
    ]
    return _emit(body, kind="ctx.read", saved=max(0, full_tokens - core.est_tokens("\n".join(body))))


def cmd_outline(args: list[str]) -> int:
    """outline FILE... — definitions only, for deciding what to actually read."""
    body: list[str] = []
    saved = 0
    for target in args or ["."]:
        path = _resolve(target)
        if not path.is_file():
            continue
        saved += core.est_tokens(path.read_text(encoding="utf-8", errors="replace"))
        items = compress.outline(path)
        body.append(f"## {path}")
        body.extend(items or ["  (no recognisable definitions)"])
        body.append("")
    if not body:
        body = ["ctx outline: no readable files given"]
    return _emit(body, kind="ctx.outline", saved=max(0, saved - core.est_tokens("\n".join(body))))


# --- grep ---------------------------------------------------------------------


def cmd_grep(args: list[str]) -> int:
    """grep PATTERN [PATH] [--all] — matches, capped per file, ignore-aware."""
    if not args:
        print("usage: ctx grep PATTERN [PATH] [--all]", file=sys.stderr)
        return 2
    show_all = "--all" in args
    positional = [arg for arg in args if not arg.startswith("--")]
    pattern = positional[0]
    where = positional[1] if len(positional) > 1 else "."

    if shutil.which("rg"):
        argv = ["rg", "--line-number", "--no-heading", "--color", "never",
                "--max-columns", str(config.MAX_LINE_CHARS), pattern, where]
    else:
        argv = ["grep", "-rn", "--color=never"]
        argv += [f"--exclude-dir={name}" for name in sorted(config.SKIP_DIRS)]
        argv += [pattern, where]

    proc = subprocess.run(argv, capture_output=True, text=True, cwd=core.project_dir())
    if proc.returncode not in (0, 1):
        print(proc.stderr.strip()[:500], file=sys.stderr)
    raw_lines = [line for line in proc.stdout.split("\n") if line.strip()]
    raw_tokens = core.est_tokens(proc.stdout)

    per_file: dict[str, list[str]] = {}
    for line in raw_lines:
        name = line.split(":", 1)[0]
        per_file.setdefault(name, []).append(compress.clip(line))

    cap = 10_000 if show_all else config.GREP_MATCHES_PER_FILE
    body = [f"# ctx grep {pattern!r} in {where} — {len(raw_lines)} matches in {len(per_file)} files"]
    hidden = 0
    for name, matches in sorted(per_file.items()):
        body.append(f"## {name} ({len(matches)})")
        body.extend(matches[:cap])
        if len(matches) > cap:
            hidden += len(matches) - cap
            body.append(f"    … {len(matches) - cap} more in this file")
    if hidden:
        body.append("")
        body.append(f"— {hidden} matches not shown. Full set: ctx grep {shlex.quote(pattern)} {shlex.quote(where)} --all")
    return _emit(body, kind="ctx.grep", saved=max(0, raw_tokens - core.est_tokens("\n".join(body))))


# --- map ----------------------------------------------------------------------


def cmd_map(args: list[str]) -> int:
    """map [PATH] — directory shape with file counts, ignored dirs pruned."""
    where = _resolve(args[0]) if args else core.project_dir()
    files: list[Path] = []
    proc = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                          capture_output=True, text=True, cwd=where if where.is_dir() else where.parent)
    if proc.returncode == 0 and proc.stdout.strip():
        files = [Path(line) for line in proc.stdout.split("\n") if line.strip()]
    else:  # not a repo: walk, pruning the usual suspects
        for root, dirnames, filenames in os.walk(where):
            dirnames[:] = [name for name in dirnames if name not in config.SKIP_DIRS and not name.startswith(".")]
            for name in filenames:
                files.append(Path(root, name).relative_to(where))

    buckets: dict[str, dict[str, int]] = {}
    for file in files:
        # Bucket by containing directory (capped depth), never by the file itself.
        parts = file.parts[:-1][:config.MAP_DEPTH - 1]
        key = "/".join(parts) if parts else "."
        suffix = file.suffix or "(no ext)"
        buckets.setdefault(key, {})
        buckets[key][suffix] = buckets[key].get(suffix, 0) + 1

    body = [f"# map of {where} — {len(files)} tracked/untracked files, ignored dirs pruned"]
    for key in sorted(buckets):
        kinds = sorted(buckets[key].items(), key=lambda item: -item[1])[:6]
        total = sum(buckets[key].values())
        shape = ", ".join(f"{count}{suffix}" for suffix, count in kinds)
        label = key if key == "." else key + "/"
        body.append(f"  {label:<40} {total:>4} {'file ' if total == 1 else 'files'}  {shape}")
    body.append("")
    body.append("— counts only. List one directory with: rg --files <dir> | head -50")
    return _emit(body, kind="ctx.map", saved=max(0, len(files) * 12 // config.CHARS_PER_TOKEN))


# --- diff ---------------------------------------------------------------------


def cmd_diff(args: list[str]) -> int:
    """diff [git-diff args] — stat first, generated files excluded, capped body."""
    root = core.project_dir()
    exclusions = [f":(exclude,glob){pattern}" for pattern in ("**/*.lock", "**/*-lock.json", "**/*.min.*", "**/*.map")]
    stat = subprocess.run(["git", "diff", "--stat", *args], capture_output=True, text=True, cwd=root)
    full = subprocess.run(["git", "diff", *args, "--", ".", *exclusions], capture_output=True, text=True, cwd=root)
    raw_tokens = core.est_tokens(full.stdout)

    log = _cache_file("diff", ".patch")
    try:
        log.write_text(full.stdout, encoding="utf-8")
    except OSError:
        pass

    lines, dropped = compress.denoise(full.stdout)
    limit = 300
    body = ["# git diff --stat", *stat.stdout.rstrip().split("\n"), "", "# diff (generated files excluded)"]
    body.extend(lines[:limit])
    if len(lines) > limit:
        body.append(f"— {len(lines) - limit} diff lines not shown; complete patch: {log}")
        body.append(f"    read a slice with: ctx read {log} {limit} {limit + 300}")
    if dropped:
        body.append(f"— {dropped} noise/duplicate lines folded")
    return _emit(body, kind="ctx.diff", saved=max(0, raw_tokens - core.est_tokens("\n".join(body))))


# --- run ----------------------------------------------------------------------


def cmd_run(args: list[str]) -> int:
    """run -- CMD... — run it, keep the full log on disk, print only the signal."""
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("usage: ctx run -- COMMAND", file=sys.stderr)
        return 2
    command = " ".join(args)
    started = time.time()
    proc = subprocess.run(command, shell=True, cwd=core.project_dir(),
                          capture_output=True, text=True, errors="replace")
    output = (proc.stdout + proc.stderr)[:MAX_CAPTURE_BYTES]
    duration = time.time() - started
    raw_tokens = core.est_tokens(output)

    log = _cache_file("run")
    try:
        log.write_text(output, encoding="utf-8")
    except OSError:
        pass

    lines, dropped = compress.denoise(output)
    while lines and not lines[-1].strip():
        lines.pop()
    header = [
        f"$ {command}",
        f"exit={proc.returncode}  time={duration:.1f}s  {len(lines)} output lines  full log: {log}",
        "",
    ]

    if len(lines) <= 60:
        # Short enough that selecting would cost more than it saves.
        body = [*header, *lines]
    elif proc.returncode == 0:
        shown = lines[-config.TAIL_LINES:]
        body = [*header, *shown,
                f"— passed; {len(lines) - len(shown)} earlier lines omitted (all of them in {log})"]
    else:
        # One ordered selection: the opening lines, every error neighbourhood,
        # and the tail — merged so nothing is printed twice.
        keep = set(range(min(8, len(lines))))
        keep |= set(compress.error_window(lines))
        keep |= set(range(max(0, len(lines) - 20), len(lines)))
        picked: list[str] = []
        previous = -1
        for index in sorted(keep):
            gap = index - previous - 1
            if gap > 0:
                picked.append(f"      ⋮ ({gap} line{'' if gap == 1 else 's'} omitted)")
            picked.append(lines[index])
            previous = index
        body = [*header, *picked, "",
                f"— failed; showing {len(keep)} of {len(lines)} lines (opening, every error context, tail).",
                f"— complete log: ctx read {log}"]
    if dropped:
        body.append(f"— {dropped} progress/duplicate lines folded")
    _emit(body, kind="ctx.run", saved=max(0, raw_tokens - core.est_tokens("\n".join(body))))
    return proc.returncode


# --- json ---------------------------------------------------------------------


def _shape(value: Any, depth: int = 0, max_depth: int = 3) -> Any:
    if isinstance(value, dict):
        if depth >= max_depth:
            return f"{{…{len(value)} keys}}"
        return {key: _shape(item, depth + 1, max_depth) for key, item in list(value.items())[:40]}
    if isinstance(value, list):
        if not value:
            return "[]"
        return [f"list[{len(value)}] of", _shape(value[0], depth + 1, max_depth)]
    if isinstance(value, str):
        return f"str({len(value)})" if len(value) > 40 else value
    return type(value).__name__ if value is None else value


def cmd_json(args: list[str]) -> int:
    """json FILE — schema and sizes instead of a megabyte of data."""
    if not args:
        print("usage: ctx json FILE", file=sys.stderr)
        return 2
    path = _resolve(args[0])
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as error:
        print(f"ctx json: {error}", file=sys.stderr)
        return 1
    raw_tokens = core.est_tokens(path.read_text(encoding="utf-8", errors="replace"))
    body = [
        f"# {path} — structure only ({path.stat().st_size} bytes)",
        json.dumps(_shape(data), indent=2)[:6000],
        "",
        f"— values elided. Query the real thing: jq '<path>' {path}",
    ]
    return _emit(body, kind="ctx.json", saved=max(0, raw_tokens - core.est_tokens("\n".join(body))))


# --- control ------------------------------------------------------------------


def cmd_stats(_args: list[str]) -> int:
    """stats — the savings ledger (estimates, never billed numbers)."""
    totals = core.totals()
    by_kind = totals.by_kind or {}
    # An interception's baseline is what the naive form would have cost:
    # what we avoided, plus what the cheap form still delivered.
    avoided = sum(value for kind, value in by_kind.items() if not kind.startswith("spent."))
    delivered = sum(
        value for kind, value in _spent_by_kind().items() if kind.startswith("ctx.")
    )
    observed = sum(value for kind, value in _spent_by_kind().items() if kind.startswith("spent."))
    baseline = avoided + delivered
    share = (avoided / baseline * 100) if baseline else 0.0
    ranked = sorted(
        ((kind, value) for kind, value in by_kind.items() if not kind.startswith("spent.") and value),
        key=lambda item: -item[1],
    )
    body = [
        "# token-saver ledger  (estimates at ~4 chars/token, not billing data)",
        f"  interceptions/compressions   {totals.events:,} events",
        f"  naive baseline               {baseline:,} tokens",
        f"  actually delivered           {delivered:,} tokens",
        f"  avoided                      {avoided:,} tokens  ({share:.0f}% of baseline)",
        f"  tool output seen this repo   {observed:,} tokens",
        "",
        "## avoided, by source",
        *[f"  {kind:<26} {value:>10,}" for kind, value in ranked],
        "",
        f"  ledger: {core.ledger_path()}",
    ]
    print("\n".join(body))
    return 0


def _spent_by_kind() -> dict[str, int]:
    """Per-kind `spent` totals (core.totals only aggregates `saved`)."""
    spent: dict[str, int] = {}
    try:
        with core.ledger_path().open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = str(row.get("kind", "?"))
                spent[kind] = spent.get(kind, 0) + int(row.get("spent", 0))
    except OSError:
        pass
    return spent


def cmd_off(_args: list[str]) -> int:
    (core.state_dir() / config.DISABLE_FLAG).write_text("off", encoding="utf-8")
    print("token-saver: OFF (hooks pass everything through). Re-enable with: ctx on")
    return 0


def cmd_on(_args: list[str]) -> int:
    flag = core.state_dir() / config.DISABLE_FLAG
    if flag.exists():
        flag.unlink()
    print("token-saver: ON")
    return 0


def cmd_status(_args: list[str]) -> int:
    from . import daemon
    print(f"enabled : {core.enabled()}")
    print(f"project : {core.project_dir()}")
    print(f"state   : {core.state_dir()}")
    print(f"daemon  : {daemon.status()}")
    return cmd_stats([])


def cmd_daemon(args: list[str]) -> int:
    from . import daemon
    action = args[0] if args else "status"
    if action == "start":
        daemon.ensure_running()
        print(f"daemon: {daemon.status()}")
    elif action == "stop":
        daemon.stop()
        print("daemon: stopped")
    elif action == "run":  # foreground, used by the spawned process itself
        daemon.run_forever()
    else:
        print(f"daemon: {daemon.status()}")
    return 0


COMMANDS = {
    "read": cmd_read, "outline": cmd_outline, "grep": cmd_grep, "map": cmd_map,
    "diff": cmd_diff, "run": cmd_run, "json": cmd_json, "stats": cmd_stats,
    "status": cmd_status, "on": cmd_on, "off": cmd_off, "daemon": cmd_daemon,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        print("commands:")
        for name, func in COMMANDS.items():
            summary = (func.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:<8} {summary}")
        return 0
    command = COMMANDS.get(argv[0])
    if not command:
        print(f"ctx: unknown command {argv[0]!r} (try `ctx help`)", file=sys.stderr)
        return 2
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main())
