"""Loss-announcing compressors.

Every function here may drop text, but never silently: whatever is removed is
counted and the caller prints a one-line pointer to the full copy. That is the
whole quality contract — the model can always ask for the rest.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")
PROGRESS_RE = re.compile(r"^[\s\d.%|/\\=<>#*\[\]-]*$")
ERROR_RE = re.compile(
    r"\b(error|errors|failed|failure|failures|exception|traceback|assert|"
    r"assertionerror|fatal|panic|cannot|unable to|not found|undefined|"
    r"syntaxerror|typeerror|valueerror|segfault|timeout|refused|denied)\b",
    re.IGNORECASE,
)

#: Definition lines per language family. Picking by suffix keeps an outline
#: from filling up with, say, Python comments that look like markdown headings.
CODE_OUTLINE_RE = re.compile(
    r"^\s*("
    r"(async\s+)?def\s+\w+|class\s+\w+|"                        # python
    r"(export\s+)?(default\s+)?(async\s+)?function\s+\w+|"      # js/ts
    r"(export\s+)?(abstract\s+)?class\s+\w+|"                    # js/ts/java
    r"(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?\(|"    # js arrow fns
    r"(export\s+)?(interface|type|enum)\s+\w+|"                   # ts
    r"func\s+(\(\w[^)]*\)\s*)?\w+|"                              # go
    r"(pub\s+)?(fn|struct|trait|impl|enum)\s+\w+|"                # rust
    r"(public|private|protected)\s+[\w<>\[\], ]+\s+\w+\s*\("      # java/c#
    r")"
)

MARKDOWN_OUTLINE_RE = re.compile(r"^#{1,4}\s+\S")

#: Top-level and second-level keys only — deeper nesting is detail, not shape.
YAML_OUTLINE_RE = re.compile(r"^(\w[\w .-]*|  \w[\w .-]*):(\s|$)")

OUTLINE_BY_SUFFIX = {
    ".md": MARKDOWN_OUTLINE_RE,
    ".markdown": MARKDOWN_OUTLINE_RE,
    ".yaml": YAML_OUTLINE_RE,
    ".yml": YAML_OUTLINE_RE,
}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def clip(line: str, limit: int | None = None) -> str:
    limit = limit or config.MAX_LINE_CHARS
    if len(line) <= limit:
        return line
    return f"{line[:limit]}… [+{len(line) - limit} chars]"


def is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(config.NOISE_PREFIXES):
        return True
    # Pure progress bars / dot runners carry no information.
    return len(stripped) > 3 and bool(PROGRESS_RE.match(stripped))


def collapse(lines: list[str]) -> tuple[list[str], int]:
    """Fold runs of identical lines into `line   (× N)`. Returns (lines, dropped)."""
    out: list[str] = []
    dropped = 0
    index = 0
    while index < len(lines):
        run = 1
        while index + run < len(lines) and lines[index + run] == lines[index]:
            run += 1
        if run > 2:
            out.append(f"{lines[index]}   (× {run})")
            dropped += run - 1
        else:
            out.extend(lines[index:index + run])
        index += run
    return out, dropped


def denoise(text: str) -> tuple[list[str], int]:
    """Strip ANSI, drop known chatter, fold repeats. Returns (lines, dropped)."""
    lines = [clip(line) for line in strip_ansi(text).split("\n")]
    kept = [line for line in lines if not is_noise(line)]
    dropped = len(lines) - len(kept)
    kept, folded = collapse(kept)
    return kept, dropped + folded


def error_window(lines: list[str], context: int = 3) -> list[int]:
    """Indices of error lines plus their surrounding context, in order."""
    hits: set[int] = set()
    for index, line in enumerate(lines):
        if ERROR_RE.search(line):
            hits.update(range(max(0, index - context), min(len(lines), index + context + 1)))
    return sorted(hits)


def outline(path: Path, max_items: int = 80) -> list[str]:
    """Structural skeleton of a file: `LINE: definition`."""
    pattern = OUTLINE_BY_SUFFIX.get(path.suffix.lower(), CODE_OUTLINE_RE)
    items: list[str] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if pattern.match(line.rstrip("\n")):
                    items.append(f"{number:>6}: {clip(line.rstrip(), 120)}")
                    if len(items) >= max_items:
                        items.append("       … outline truncated")
                        break
    except OSError:
        return []
    return items


def numbered(lines: list[str], start: int = 1) -> list[str]:
    return [f"{start + offset:>6}\t{clip(line)}" for offset, line in enumerate(lines)]
