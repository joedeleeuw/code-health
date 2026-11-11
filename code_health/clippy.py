"""cargo clippy on each tracked Cargo workspace, keyed per lint and file like
the ast-grep rules.

Clippy supplies receiver and type provenance; rustc supplies dead-code and
unused diagnostics. code_health/config/clippy.toml is the single owner of
disallowed-methods and disallowed-types for this pass; each workspace is linted
with that file plus the repository's complexity, nesting and parameter limits
as Clippy thresholds, passed through CLIPPY_CONF_DIR. Other Clippy lints are allowed and lint levels
are capped at warn. Each selected diagnostic is counted once at its primary
span. --locked prevents Cargo.lock changes. Missing tools and compilation
failures stop the check.
"""

import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from code_health import run
from code_health.config import Limits
from pydantic import BaseModel, ValidationError

CONFIG_DIR = Path(__file__).resolve().parent / "config"
LINTS = (
    "clippy::disallowed_methods",
    "clippy::disallowed_types",
    "clippy::let_underscore_must_use",
    "clippy::let_underscore_lock",
    "clippy::map_err_ignore",
    "clippy::manual_unwrap_or_default",
    "clippy::await_holding_lock",
    "clippy::await_holding_refcell_ref",
    "clippy::allow_attributes_without_reason",
    "clippy::cognitive_complexity",
    "clippy::excessive_nesting",
    "clippy::too_many_arguments",
    "dead_code",
    "unused_imports",
    "unused_variables",
    "unused_must_use",
)
CODES = {lint: lint.removeprefix("clippy::") for lint in LINTS}
MANIFEST = "Cargo.toml"


class Span(BaseModel):
    file_name: str
    line_start: int
    is_primary: bool


class Code(BaseModel):
    code: str


class Diagnostic(BaseModel):
    code: Code | None
    message: str
    spans: list[Span]


class CargoMessage(BaseModel):
    reason: str
    message: Diagnostic | None = None


def workspaces(files: list[str]) -> list[PurePosixPath]:
    """Each measured manifest directory with no measured manifest above it."""
    folders = sorted(
        PurePosixPath(path).parent
        for path in files
        if PurePosixPath(path).name == MANIFEST
    )
    return [
        folder
        for folder in folders
        if not any(other in folder.parents for other in folders)
    ]


def diagnostics(output: str) -> list[Diagnostic]:
    found = []
    for number, line in enumerate(output.splitlines(), 1):
        try:
            cargo = CargoMessage.model_validate_json(line)
        except ValidationError as error:
            raise SystemExit(
                f"code-health: invalid cargo output line {number}: {error}"
            ) from error
        if cargo.message is not None:
            found.append(cargo.message)
    return found


def cargo_clippy(root: Path, files: list[str], limits: Limits) -> dict[str, list[str]]:
    """Lint matches per lint and measured file."""
    with TemporaryDirectory() as folder:
        thresholds = (
            f"cognitive-complexity-threshold = {limits.complexity}\n"
            f"excessive-nesting-threshold = {limits.nesting}\n"
            f"too-many-arguments-threshold = {limits.params}\n"
        )
        packaged = (CONFIG_DIR / "clippy.toml").read_text(encoding="utf-8")
        Path(folder, "clippy.toml").write_text(thresholds + packaged, encoding="utf-8")
        return linted(root, files, {**os.environ, "CLIPPY_CONF_DIR": folder})


def linted(
    root: Path, files: list[str], environment: dict[str, str]
) -> dict[str, list[str]]:
    measured = set(files)
    found: dict[str, list[str]] = {}
    for workspace in workspaces(files):
        command = [
            "cargo",
            "clippy",
            "--locked",
            "--workspace",
            "--all-targets",
            "--no-deps",
            "--message-format=json",
            "--",
            "--cap-lints",
            "warn",
            "-A",
            "clippy::all",
        ]
        for lint in LINTS:
            command += ["-W", lint]
        output = run(command, root / workspace, environment=environment)
        seen: set[tuple[str, str, int, str]] = set()
        for diagnostic in diagnostics(output):
            lint = CODES.get(diagnostic.code.code) if diagnostic.code else None
            if lint is None:
                continue
            for span in diagnostic.spans:
                file = str(workspace / span.file_name)
                record = (lint, file, span.line_start, diagnostic.message)
                if not span.is_primary or file not in measured or record in seen:
                    continue
                seen.add(record)
                found.setdefault(f"{lint}::{file}", []).append(
                    f"line {span.line_start}: {diagnostic.message}"
                )
    return found
