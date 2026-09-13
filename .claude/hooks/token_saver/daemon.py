"""The always-on part: a tiny maintenance process.

It exists so that the expensive preparation work (repo map, cache hygiene)
never happens on the critical path of a tool call. It is deliberately boring:
one short pass every DAEMON_INTERVAL seconds, no watching, no threads, and a
hard idle timeout so a forgotten process cannot outlive its usefulness.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, core


def pid_file() -> Path:
    return core.state_dir() / "daemon.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def current_pid() -> int | None:
    try:
        pid = int(pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def status() -> str:
    pid = current_pid()
    return f"running (pid {pid})" if pid else "stopped"


def ensure_running() -> bool:
    """Start the daemon if it is not already up. Never raises."""
    if not core.enabled() or current_pid():
        return False
    try:
        log = core.state_dir() / "logs" / "daemon.log"
        handle = log.open("a", encoding="utf-8")
        package_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=package_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.Popen(
            [sys.executable, "-m", "token_saver.ctx", "daemon", "run"],
            cwd=core.project_dir(), env=env, stdout=handle, stderr=handle,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        pid_file().write_text(str(proc.pid), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return False


def stop() -> None:
    pid = current_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        pid_file().unlink()
    except OSError:
        pass


# --- maintenance passes -------------------------------------------------------


def refresh_map() -> None:
    """Keep a fresh repo map on disk so no session has to compute one."""
    root = core.project_dir()
    marker = root / ".git" / "index"
    cache = core.state_dir() / "cache" / "map.txt"
    try:
        stamp = str(int(marker.stat().st_mtime)) if marker.exists() else str(int(time.time() // 600))
    except OSError:
        stamp = "0"
    stamp_file = core.state_dir() / "cache" / "map.stamp"
    if cache.exists() and core.read_json(stamp_file, None) == stamp:
        return
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "token_saver.ctx", "map"],
            cwd=root, capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent)),
        )
        if proc.returncode == 0:
            cache.write_text(proc.stdout, encoding="utf-8")
            core.write_json(stamp_file, stamp)
    except (OSError, subprocess.SubprocessError):
        pass


def prune_cache() -> None:
    """Delete logs, patches and session memory nobody will ask for again."""
    cutoff = time.time() - config.CACHE_TTL_SECONDS
    for folder in ("logs", "cache", "sessions"):
        directory = core.state_dir() / folder
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff and entry.name != "daemon.log":
                    entry.unlink()
            except OSError:
                continue


def trim_ledger(max_lines: int = 20_000) -> None:
    """Keep the ledger bounded; totals stay honest because we keep the tail."""
    path = core.ledger_path()
    try:
        if not path.exists() or path.stat().st_size < 4_000_000:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        path.write_text("\n".join(lines[-max_lines:]), encoding="utf-8")
    except OSError:
        pass


def run_forever() -> None:
    """Foreground loop of the background process."""
    core.touch_activity()
    while True:
        if not core.enabled():
            break
        refresh_map()
        prune_cache()
        trim_ledger()
        if time.time() - core.last_activity() > config.DAEMON_IDLE_EXIT:
            break
        time.sleep(config.DAEMON_INTERVAL)
    try:
        pid_file().unlink()
    except OSError:
        pass
