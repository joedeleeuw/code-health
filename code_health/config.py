"""Decode the repository's settings: the [tool.code-health] table of its
pyproject.toml. A repository without the file or the table gets the defaults."""

import fnmatch
import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from code_health import stop
from code_health.checks import CHECKS, CONFIGURED
from code_health.rule_ledger import refuse_unknown_rules

CONFIG = "pyproject.toml"
TABLE = "tool.code-health"
Count = Annotated[int, Field(strict=True, ge=0)]


DEFAULT_LIMITS = dict(zip(CONFIGURED, (1000, 25, 4, 5, 0), strict=True))
Limits = create_model(
    "Limits",
    __config__=ConfigDict(extra="forbid", strict=True),
    **{check: (Count, limit) for check, limit in DEFAULT_LIMITS.items()},
)


class CodeHealthConfig(BaseModel, extra="forbid", strict=True):
    exclude: list[str] = []
    allowed: dict[str, list[str]] = {}
    limits: Limits = Limits()


def nix_lint(check: str) -> bool:
    return check == "deadnix" or check.startswith("statix-")


def load_config(root: Path) -> CodeHealthConfig:
    path = root / CONFIG
    if not path.exists():
        return CodeHealthConfig()
    try:
        document = tomllib.loads(path.read_text())
    except OSError as error:
        stop(f"{CONFIG}: {error.strerror}")
    except tomllib.TOMLDecodeError as error:
        stop(f"{CONFIG}: {error}")
    table = document.get("tool", {}).get("code-health", {})
    try:
        config = CodeHealthConfig.model_validate(table)
    except ValidationError as error:
        problems = [
            f"{CONFIG} [{TABLE}] {'.'.join(map(str, problem['loc']))}: {problem['msg']}"
            for problem in error.errors(include_input=False, include_url=False)
        ]
        stop("\ncode-health: ".join(problems))
    rules = [
        check
        for check in sorted(config.allowed)
        if check not in CHECKS and not nix_lint(check)
    ]
    refuse_unknown_rules(f"{CONFIG} [{TABLE}] allowed", rules)
    return config


def refuse_unmatched_allowances(
    allowed: dict[str, list[str]], files: list[str]
) -> None:
    for check, globs in sorted(allowed.items()):
        for glob in globs:
            if not any(fnmatch.fnmatch(path, glob) for path in files):
                stop(
                    f"{CONFIG} [{TABLE}] allowed.{check}: "
                    f"{glob} matches no measured file"
                )
