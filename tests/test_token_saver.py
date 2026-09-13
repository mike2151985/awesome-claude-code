"""Tests for the background token saver (.claude/hooks/token_saver).

The suite pins the two properties the feature is only worth having if it keeps:
every interception names an equivalent command, and nothing is dropped without
the output saying where the rest is.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".claude" / "hooks"))

from token_saver import compress, core, ctx, hook, rules  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point the saver at a throwaway project + state directory."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv("TOKEN_SAVER_STATE", str(tmp_path / "state"))
    monkeypatch.delenv("TOKEN_SAVER", raising=False)
    monkeypatch.chdir(project)
    return project


def run_hook(action: str, payload: dict) -> dict | None:
    """Invoke a hook in-process and parse whatever decision it printed."""
    buffer = io.StringIO()
    monkey_stdin = io.StringIO(json.dumps(payload))
    original = sys.stdin
    sys.stdin = monkey_stdin
    try:
        with redirect_stdout(buffer):
            assert hook.main([action]) == 0
    finally:
        sys.stdin = original
    output = buffer.getvalue().strip()
    return json.loads(output) if output else None


def decision(result: dict | None) -> str | None:
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def reason(result: dict | None) -> str:
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")


def big_file(project: Path, name: str = "big.py", lines: int = 900) -> Path:
    path = project / name
    path.write_text("\n".join(f"value_{index} = {index}" for index in range(lines)), encoding="utf-8")
    return path


# --- rules --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected_rule"),
    [
        ("git log", "unbounded-git-log"),
        ("git status", "verbose-git-status"),
        ("grep -r needle .", "recursive-grep"),
        ("ls -R", "tree-dump"),
        ("pytest tests/", "noisy-runner"),
        ("pip freeze", "dependency-dump"),
        ("journalctl -u nginx", "unbounded-logs"),
    ],
)
def test_expensive_commands_get_a_cheaper_equivalent(command, expected_rule):
    redirect = rules.analyze(command)
    assert redirect is not None and redirect.rule == expected_rule
    # The message must always hand back a runnable replacement.
    assert redirect.command.strip()
    assert redirect.command in redirect.message()


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -n 10",
        "git status --short",
        "grep -rn needle . | head -20",          # already bounded by a pipe
        "sed -n '1,40p' file.py",
        "echo hello",
        "npm run build > build.log",             # output never reaches the context
        "TOKEN_SAVER=off cat huge.log",          # explicit opt-out
    ],
)
def test_cheap_or_opted_out_commands_are_untouched(command):
    assert rules.analyze(command) is None


def test_small_file_dump_is_allowed(sandbox):
    (sandbox / "small.py").write_text("print('hi')\n", encoding="utf-8")
    assert rules.analyze("cat small.py") is None


def test_large_file_dump_is_redirected(sandbox):
    big_file(sandbox)
    redirect = rules.analyze("cat big.py")
    assert redirect is not None and redirect.rule == "big-file-dump"
    assert redirect.saved_tokens > 0


def test_generated_file_dump_is_redirected(sandbox):
    (sandbox / "package-lock.json").write_text('{"a": 1}\n', encoding="utf-8")
    redirect = rules.analyze("cat package-lock.json")
    assert redirect is not None and "generated" in redirect.reason


# --- compression --------------------------------------------------------------


def test_repeated_lines_are_folded_with_a_count():
    lines, dropped = compress.denoise("x\nx\nx\nx\ny")
    assert lines == ["x   (× 4)", "y"] and dropped == 3


def test_ansi_and_install_chatter_are_dropped():
    lines, _ = compress.denoise("\x1b[32mok\x1b[0m\nRequirement already satisfied: six\nreal line")
    assert lines == ["ok", "real line"]


def test_outline_uses_language_appropriate_patterns(sandbox):
    python_file = sandbox / "mod.py"
    python_file.write_text("# heading-ish comment\ndef alpha():\n    pass\n", encoding="utf-8")
    items = compress.outline(python_file)
    assert any("def alpha" in item for item in items)
    assert not any("heading-ish" in item for item in items)


def test_long_lines_are_clipped_with_a_marker():
    clipped = compress.clip("y" * 900, limit=50)
    assert clipped.startswith("y" * 50) and "+850 chars" in clipped


# --- ctx ----------------------------------------------------------------------


def capture(argv: list[str]) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        ctx.main(argv)
    return buffer.getvalue()


def test_ctx_read_prints_a_small_file_in_full(sandbox):
    (sandbox / "small.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    output = capture(["read", "small.py"])
    assert "alpha = 1" in output and "beta = 2" in output and "complete" in output


def test_ctx_read_of_a_big_file_shows_shape_and_says_where_the_rest_is(sandbox):
    big_file(sandbox)
    output = capture(["read", "big.py"])
    assert "outline" in output
    assert "lines not shown" in output
    assert "ctx read" in output                      # the retrieval command is spelled out
    assert len(output) < (sandbox / "big.py").stat().st_size


def test_ctx_read_window_is_exact(sandbox):
    (sandbox / "f.txt").write_text("\n".join(str(index) for index in range(1, 51)), encoding="utf-8")
    output = capture(["read", "f.txt", "10", "12"])
    assert "\t10" in output and "\t12" in output and "\t13" not in output


def test_ctx_run_reports_failures_and_keeps_the_full_log(sandbox):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = ctx.main(["run", "--", "echo before; echo 'ERROR: exploded'; exit 7"])
    output = buffer.getvalue()
    assert code == 7                                  # exit status is never swallowed
    assert "ERROR: exploded" in output
    log_line = [line for line in output.split("\n") if "full log:" in line][0]
    log_path = Path(log_line.split("full log:")[1].strip())
    assert log_path.is_file() and "ERROR: exploded" in log_path.read_text(encoding="utf-8")


def test_ctx_grep_caps_per_file_matches_and_offers_the_full_set(sandbox):
    (sandbox / "many.txt").write_text("\n".join(["needle here"] * 40), encoding="utf-8")
    output = capture(["grep", "needle", "."])
    assert "more in this file" in output and "--all" in output
    assert output.count("needle here") <= 40


def test_ctx_map_groups_by_directory(sandbox):
    (sandbox / "pkg").mkdir()
    (sandbox / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox / "top.md").write_text("# hi\n", encoding="utf-8")
    output = capture(["map"])
    assert "pkg/" in output and "1.py" in output and "1.md" in output


def test_ledger_accumulates_and_stats_render(sandbox):
    core.record("rule.test", saved_tokens=1000, detail="x")
    output = capture(["stats"])
    assert "1,000" in output and "avoided" in output


def test_off_switch_disables_every_hook(sandbox):
    capture(["off"])
    assert core.enabled() is False
    big_file(sandbox)
    assert run_hook("pre-bash", {"session_id": "s", "tool_input": {"command": "cat big.py"}}) is None
    capture(["on"])
    assert core.enabled() is True


# --- hooks --------------------------------------------------------------------


def test_pre_bash_denies_with_a_replacement_command(sandbox):
    big_file(sandbox)
    result = run_hook("pre-bash", {"session_id": "s1", "tool_input": {"command": "cat big.py"}})
    assert decision(result) == "deny"
    assert "ctx" in reason(result) and "TOKEN_SAVER=off" in reason(result)


def test_pre_bash_deduplicates_identical_reads_of_unchanged_files(sandbox):
    target = sandbox / "note.txt"
    target.write_text("hello\n", encoding="utf-8")
    payload = {"session_id": "s2", "tool_input": {"command": "cat note.txt"}}
    assert run_hook("pre-bash", payload) is None            # first time: pass through
    assert decision(run_hook("pre-bash", payload)) == "deny"  # byte-identical repeat


def test_editing_a_file_reopens_it_for_reading(sandbox):
    target = sandbox / "note.txt"
    target.write_text("hello\n", encoding="utf-8")
    payload = {"session_id": "s3", "tool_input": {"command": "cat note.txt"}}
    run_hook("pre-bash", payload)
    target.write_text("hello\nworld\n", encoding="utf-8")     # content changed
    assert run_hook("pre-bash", payload) is None


def test_pre_read_blocks_whole_file_reads_of_big_files_only(sandbox):
    big_file(sandbox)
    (sandbox / "small.py").write_text("x = 1\n", encoding="utf-8")
    blocked = run_hook("pre-read", {"session_id": "s4", "tool_input": {"file_path": str(sandbox / "big.py")}})
    assert decision(blocked) == "deny" and "offset/limit" in reason(blocked)
    windowed = run_hook("pre-read", {
        "session_id": "s4",
        "tool_input": {"file_path": str(sandbox / "big.py"), "offset": 100, "limit": 50},
    })
    assert windowed is None                                   # windowed reads always allowed
    assert run_hook("pre-read", {"session_id": "s4", "tool_input": {"file_path": str(sandbox / "small.py")}}) is None


def test_images_and_notebooks_are_never_intercepted(sandbox):
    image = sandbox / "shot.png"
    image.write_bytes(b"\x89PNG" + b"0" * 200_000)
    assert run_hook("pre-read", {"session_id": "s5", "tool_input": {"file_path": str(image)}}) is None


def test_post_tool_is_silent_and_sizes_the_dedupe_entry(sandbox):
    target = sandbox / "note.txt"
    target.write_text("hello\n", encoding="utf-8")
    payload = {"session_id": "s6", "tool_input": {"command": "cat note.txt"}}
    run_hook("pre-bash", payload)
    assert run_hook("post-tool", {**payload, "tool_name": "Bash", "tool_response": "hello\n" * 500}) is None
    memory = core.SessionMemory("s6")
    assert memory.data["entries"]["bash:cat note.txt"]["tokens"] > 0


@pytest.mark.parametrize("payload", [{}, {"tool_input": None}, {"tool_input": {"command": None}}])
def test_hooks_never_raise_on_malformed_events(payload):
    for action in ("pre-bash", "pre-read", "post-tool"):
        buffer = io.StringIO()
        original, sys.stdin = sys.stdin, io.StringIO(json.dumps(payload))
        try:
            with redirect_stdout(buffer):
                assert hook.main([action]) == 0
        finally:
            sys.stdin = original


def test_launchers_run_as_scripts(sandbox):
    proc = subprocess.run(
        [sys.executable, str(REPO / ".claude" / "hooks" / "ctx"), "help"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0 and "commands:" in proc.stdout


def test_a_denied_command_is_not_remembered_as_delivered(sandbox):
    """A blocked call produced no output, so it must not trigger the repeat guard."""
    big_file(sandbox)
    payload = {"session_id": "s7", "tool_input": {"command": "cat big.py"}}
    assert decision(run_hook("pre-bash", payload)) == "deny"
    assert decision(run_hook("pre-bash", payload)) == "deny"      # same advice, not "already read"
    assert "repeat" not in reason(run_hook("pre-bash", payload))
