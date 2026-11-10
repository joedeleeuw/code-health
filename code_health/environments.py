"""The installed packages a repository's Python runs against, so that a name a
library provides resolves to the library's own declaration.

An environment is a virtual environment a project folder holds: a directory
with a pyvenv.cfg directly in the root or in a folder that declares a project.
Environments are not tracked, so a folder that holds none in a linked worktree
is read with those the same folder holds in the repository's primary checkout.
A folder that holds none in either and has a uv.lock gets the environment uv
builds from that lock, as running the project would. A measured script that
declares inline dependencies runs in the environment uv keeps for it, built
the same way when it is missing.

The first environment in path order supplies the interpreter, and with it the
standard library's version; a repository with none is read with the interpreter
running the checker. An import no environment provides is reported by the
resolver, so a missing environment shows as findings rather than as silence.
"""

import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from code_health import run, stop

PROJECT_FILES = ("pyproject.toml", "setup.cfg", "setup.py")
COMMENT_LINES = r"(?:^#(?: .*)?$\s)*?"
INLINE_DEPENDENCIES = re.compile(
    rf"(?m)^# /// script$\s{COMMENT_LINES}^# dependencies = .*$\s{COMMENT_LINES}^# ///$"
)


@dataclass(frozen=True)
class Environments:
    interpreter: str
    packages: list[str]


def installed(environment: Path) -> str:
    """The directory an environment installs packages into."""
    found = sorted(environment.glob("lib/python*/site-packages"))
    if len(found) != 1:
        stop(f"{environment}: expected one lib/python*/site-packages, found {found}")
    return str(found[0])


def held(folder: Path) -> list[Path]:
    """The virtual environments directly in a folder."""
    if not folder.is_dir():
        return []
    return sorted(
        child for child in folder.iterdir() if (child / "pyvenv.cfg").is_file()
    )


def project_environments(root: Path, primary: Path, folder: str) -> list[Path]:
    """The environments of one project folder of the checkout at root."""
    found = held(root / folder) or held(primary / folder)
    if not found and (root / folder / "uv.lock").is_file():
        run(["uv", "sync", "--quiet", "--locked", "--project", folder or "."], root)
        found = held(root / folder)
    return found


def environments(
    root: Path, listed: list[str], sources: dict[str, str]
) -> Environments:
    """The environments of a checkout whose tracked paths are listed and whose
    measured Python sources are given."""
    folders = {""} | {
        posixpath.dirname(path)
        for path in listed
        if posixpath.basename(path) in PROJECT_FILES
    }
    worktrees = run(["git", "worktree", "list", "--porcelain"], root)
    primary = Path(worktrees.split("\n", 1)[0].removeprefix("worktree "))
    found = [
        environment
        for folder in sorted(folders)
        for environment in project_environments(root, primary, folder)
    ]
    interpreter = str(found[0] / "bin/python") if found else sys.executable
    packages = [installed(environment) for environment in found]
    for path, text in sorted(sources.items()):
        if INLINE_DEPENDENCIES.search(text):
            run(["uv", "sync", "--quiet", "--script", path], root)
            python = run(["uv", "python", "find", "--script", path], root).strip()
            packages.append(installed(Path(python).parent.parent))
    return Environments(interpreter, list(dict.fromkeys(packages)))
