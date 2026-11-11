"""Native Nix lint findings on the measured files."""

import subprocess
import sys
from pathlib import Path
from typing import Literal

from code_health import ERROR_EXIT, run
from pydantic import AliasPath, BaseModel, Field, ValidationError


class StatixDiagnostic(BaseModel):
    line: int = Field(validation_alias=AliasPath("at", "from", "line"))
    message: str


class StatixReport(BaseModel):
    code: int
    severity: Literal["Warn", "Error"]
    note: str
    diagnostics: list[StatixDiagnostic]


class StatixOutput(BaseModel):
    report: list[StatixReport]


class DeadnixDiagnostic(BaseModel):
    line: int
    message: str


class DeadnixOutput(BaseModel):
    diagnostics: list[DeadnixDiagnostic] = Field(validation_alias="results")


def nix_lints(root: Path, files: list[str]) -> dict[str, list[str]]:
    findings: dict[str, list[str]] = {}
    for file in files:
        if not file.endswith(".nix"):
            continue
        target = str(root / file)
        checked = subprocess.run(
            ["statix", "check", "--unrestricted", "--format", "json", target],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            checked.stderr.strip()
            or checked.returncode not in (0, 1)
            or (checked.returncode == 1 and not checked.stdout.strip())
        ):
            print(
                f"code-health: statix: {file}: exited {checked.returncode}: "
                f"{checked.stderr.strip()}",
                file=sys.stderr,
            )
            raise SystemExit(ERROR_EXIT)
        output = checked.stdout
        try:
            reports = (
                StatixOutput.model_validate_json(output, strict=True).report
                if output.strip()
                else []
            )
            for report in reports:
                if report.severity == "Error":
                    print(
                        f"code-health: statix: {file}: {report.note}", file=sys.stderr
                    )
                    raise SystemExit(ERROR_EXIT)
                details = "; ".join(
                    f"line {diagnostic.line}: {diagnostic.message}"
                    for diagnostic in report.diagnostics
                )
                findings.setdefault(f"statix-{report.code}::{file}", []).append(
                    f"{report.note}: {details}"
                )
            output = run(["deadnix", "--output-format", "json", target], root)
            diagnostics = (
                DeadnixOutput.model_validate_json(output, strict=True).diagnostics
                if output.strip()
                else []
            )
            for diagnostic in diagnostics:
                findings.setdefault(f"deadnix::{file}", []).append(
                    f"line {diagnostic.line}: {diagnostic.message}"
                )
        except ValidationError as error:
            print(f"code-health: Nix diagnostics for {file}: {error}", file=sys.stderr)
            raise SystemExit(ERROR_EXIT) from error
    return findings
