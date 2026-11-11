"""Measurements behind the code-health checks."""

import subprocess
import sys
from pathlib import Path
from typing import NoReturn

ERROR_EXIT = 2


def stop(message: str) -> NoReturn:
    print(f"code-health: {message}", file=sys.stderr)
    sys.exit(ERROR_EXIT)


def run(
    command: list[str],
    root: Path,
    ok=(0,),
    stdin: str = "",
    environment: dict[str, str] | None = None,
) -> str:
    done = subprocess.run(
        command,
        cwd=root,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    if done.returncode not in ok:
        reason = done.stderr.strip()
        stop(f"{command[0]} exited {done.returncode}: {reason}")
    return done.stdout


def counted(findings: dict[str, list[str]]) -> dict[str, int]:
    return {key: len(found) for key, found in findings.items()}


def keyed(rows: list[tuple[str, int]]) -> dict[str, int]:
    """Numbers repeated names within a file in source order, so keys are unique."""
    seen: dict[str, int] = {}
    out: dict[str, int] = {}
    for key, value in rows:
        seen[key] = seen.get(key, 0) + 1
        out[key if seen[key] == 1 else f"{key}#{seen[key]}"] = value
    return out
