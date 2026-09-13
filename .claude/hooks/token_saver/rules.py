"""Bash-command analysis: spot the expensive form, name the cheap equivalent.

A rule fires only when a strictly equivalent command returns the same
information for fewer tokens. If a command's output is already bounded (piped
into head/grep/wc, redirected to a file, ...) no rule fires — the interception
would cost more than it saves.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config, core

#: Stages that already cap what reaches the context window.
BOUNDING_STAGES = frozenset({
    "head", "tail", "wc", "grep", "rg", "jq", "awk", "sed", "cut", "uniq",
    "sort", "column", "diffstat", "ctx", "fzf", "less", "tee",
})

#: Commands whose output is a file dump.
DUMPERS = frozenset({"cat", "bat", "batcat", "more", "less", "nl"})

#: Test/build runners whose logs are 90% chatter.
RUNNERS = (
    "pytest", "py.test", "tox", "nox", "jest", "vitest", "mocha", "phpunit",
    "gradle", "./gradlew", "mvn", "cargo", "go", "make", "npm", "pnpm", "yarn",
    "bun", "rspec", "rake", "dotnet",
)

#: Sub-commands that make the runners above actually run something long.
RUNNER_SUBCOMMANDS = frozenset({
    "test", "build", "run", "check", "ci", "lint", "install", "compile",
    "bundle", "e2e", "coverage",
})


@dataclass
class Redirect:
    """A cheaper, information-equivalent way to run what was asked."""

    rule: str
    reason: str
    command: str
    saved_tokens: int

    def message(self) -> str:
        return (
            f"[token-saver:{self.rule}] {self.reason}\n"
            f"Run this instead (same information, ~{self.saved_tokens} fewer tokens):\n"
            f"    {self.command}\n"
            "It is an equivalent, not a shortcut: nothing is hidden without telling you "
            "where the rest is. Need the raw form anyway? Prefix the command with "
            "`TOKEN_SAVER=off ` and it runs untouched."
        )


def _segments(command: str) -> list[list[str]]:
    """Split a shell line into argv-ish segments across |, &&, ||, ;."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return []
    segments: list[list[str]] = [[]]
    for word in words:
        if word in {"|", "||", "&&", ";", "|&"}:
            segments.append([])
        else:
            segments[-1].append(word)
    return [segment for segment in segments if segment]


def _is_bounded(segments: list[list[str]], command: str) -> bool:
    """True when the output never reaches the model in full anyway."""
    if ">" in command or ">>" in command:
        return True
    for segment in segments[1:]:
        program = Path(segment[0]).name
        if program in BOUNDING_STAGES:
            return True
    return False


def _file_tokens(path: Path) -> int:
    try:
        return core.est_tokens("x" * path.stat().st_size)
    except OSError:
        return 0


def _big(path: Path) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size >= config.BIG_FILE_BYTES:
        return True
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle) > config.BIG_FILE_LINES
    except OSError:
        return False


def _ctx() -> str:
    """How to invoke the ctx CLI from a hook-suggested command line."""
    return core.ctx_command()


def analyze(command: str) -> Redirect | None:
    """Return a cheaper equivalent for `command`, or None to let it run."""
    command = command.strip()
    if not command:
        return None
    # Explicit per-command opt-out, e.g. `TOKEN_SAVER=off cat huge.log`.
    if re.match(r"^TOKEN_SAVER=(off|0|false|no)\b", command, re.IGNORECASE):
        return None

    segments = _segments(command)
    if not segments:
        return None
    if _is_bounded(segments, command):
        return None

    head = segments[0]
    program = Path(head[0]).name
    args = head[1:]
    flags = [arg for arg in args if arg.startswith("-")]
    operands = [arg for arg in args if not arg.startswith("-")]

    # 1. Dumping a large or generated file.
    if program in DUMPERS:
        files = core.existing_paths(operands)
        heavy = [path for path in files if _big(path) or core.is_generated(str(path))]
        if heavy:
            target = heavy[0]
            saved = max(0, sum(_file_tokens(path) for path in heavy) - 900)
            noun = "generated/minified" if core.is_generated(str(target)) else "large"
            return Redirect(
                rule="big-file-dump",
                reason=f"`{program} {target.name}` dumps a {noun} file ({_file_tokens(target)} est. tokens).",
                command=f"{_ctx()} read {shlex.quote(str(target))}",
                saved_tokens=saved,
            )
        if len(files) > 3:
            return Redirect(
                rule="multi-file-dump",
                reason=f"`{program}` on {len(files)} files at once floods the context.",
                command=f"{_ctx()} outline " + " ".join(shlex.quote(str(path)) for path in files),
                saved_tokens=max(0, sum(_file_tokens(path) for path in files) - 600),
            )
        return None

    # 2. head -n with an oversized window.
    if program in {"head", "tail"}:
        for index, flag in enumerate(args):
            value = None
            if flag in {"-n", "--lines"} and index + 1 < len(args):
                value = args[index + 1]
            elif flag.startswith("-n") and flag[2:].isdigit():
                value = flag[2:]
            if value and value.isdigit() and int(value) > config.BIG_FILE_LINES * 2:
                files = core.existing_paths(operands)
                name = str(files[0]) if files else (operands[-1] if operands else "FILE")
                return Redirect(
                    rule="oversized-window",
                    reason=f"`{program} -n {value}` is a whole-file dump in disguise.",
                    command=f"{_ctx()} read {shlex.quote(name)}",
                    saved_tokens=int(value) * 12 // config.CHARS_PER_TOKEN,
                )
        return None

    # 3. Recursive grep -> ripgrep with per-file caps.
    if program == "grep" and any(flag.startswith("-") and ("r" in flag or "R" in flag) for flag in flags):
        pattern = operands[0] if operands else "PATTERN"
        where = operands[1] if len(operands) > 1 else "."
        tool = "rg" if shutil.which("rg") else "grep"
        return Redirect(
            rule="recursive-grep",
            reason="Recursive grep walks node_modules/.git and prints unbounded matches.",
            command=f"{_ctx()} grep {shlex.quote(pattern)} {shlex.quote(where)}"
                    + ("" if tool == "rg" else "   # (ripgrep not installed; ctx falls back to grep)"),
            saved_tokens=1500,
        )

    # 4. find -> rg --files (respects .gitignore, no permission-denied noise).
    if program == "find" and shutil.which("rg"):
        glob: str | None = None
        for index, arg in enumerate(args):
            if arg in {"-name", "-iname"} and index + 1 < len(args):
                glob = args[index + 1]
        if glob:
            where = operands[0] if operands else "."
            return Redirect(
                rule="find-walk",
                reason="`find` descends into ignored directories and prints every hit.",
                command=f"rg --files {shlex.quote(where)} -g {shlex.quote(glob)} | head -50",
                saved_tokens=800,
            )
        return None

    # 5. Whole-tree listings.
    if (program == "ls" and any("R" in flag for flag in flags)) or (
        program == "tree" and not any(flag.startswith("-L") for flag in flags)
    ):
        where = operands[0] if operands else "."
        return Redirect(
            rule="tree-dump",
            reason=f"`{program}` here prints the entire tree including ignored directories.",
            command=f"{_ctx()} map {shlex.quote(where)}",
            saved_tokens=2000,
        )

    # 6. git: unbounded history / verbose status / raw diffs.
    if program == "git" and operands:
        sub = operands[0]
        rest = operands[1:]
        if sub == "log" and not any(
            flag.startswith("-n") or flag[1:].isdigit() or flag.startswith("--max-count")
            for flag in flags
        ):
            extra = " ".join(shlex.quote(arg) for arg in rest)
            return Redirect(
                rule="unbounded-git-log",
                reason="`git log` with no limit can print thousands of commits.",
                command=f"git log --oneline -n 20 {extra}".strip(),
                saved_tokens=1200,
            )
        if sub == "status" and not any(flag in {"-s", "--short", "--porcelain"} for flag in flags):
            return Redirect(
                rule="verbose-git-status",
                reason="Default `git status` pads the file list with hint paragraphs.",
                command="git status --short --branch",
                saved_tokens=250,
            )
        if sub in {"diff", "show"} and not any(
            flag in {"--stat", "--name-only", "--name-status", "--shortstat"} for flag in flags
        ):
            extra = " ".join(shlex.quote(arg) for arg in rest)
            return Redirect(
                rule="raw-git-diff",
                reason="A raw diff includes lockfiles and generated files; read the shape first.",
                command=f"{_ctx()} diff {extra}".strip(),
                saved_tokens=1500,
            )
        return None

    # 7. Long-running builds and test suites. (Falls through to rule 8 for the
    # sub-commands that merely list things, e.g. `npm ls`.)
    if program in RUNNERS:
        needs_subcommand = program in {"npm", "pnpm", "yarn", "bun", "go", "cargo", "dotnet", "mvn"}
        if not needs_subcommand or (operands and operands[0] in RUNNER_SUBCOMMANDS):
            return Redirect(
                rule="noisy-runner",
                reason=f"`{program}` logs are mostly progress chatter; only failures carry signal.",
                command=f"{_ctx()} run -- {command}",
                saved_tokens=2500,
            )

    # 8. Dependency listings.
    if (program == "pip" and operands and operands[0] in {"list", "freeze"}) or (
        program in {"npm", "pnpm", "yarn"} and operands and operands[0] in {"ls", "list"}
    ):
        suffix = " --depth=0" if program != "pip" else ""
        return Redirect(
            rule="dependency-dump",
            reason="Full dependency trees are hundreds of lines of rarely-read text.",
            command=f"{command}{suffix} | head -40",
            saved_tokens=900,
        )

    # 9. Log tails without a bound.
    if (program == "journalctl" or (program == "docker" and operands[:1] == ["logs"])) and not any(
        flag.startswith("-n") or flag.startswith("--lines") for flag in flags
    ):
        return Redirect(
            rule="unbounded-logs",
            reason="Log streams are unbounded by default.",
            command=f"{command} -n 200",
            saved_tokens=2000,
        )

    return None
