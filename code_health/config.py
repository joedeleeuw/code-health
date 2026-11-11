"""Decode the repository's ratchet and enforce its committed ceilings."""

import fnmatch
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_core import ErrorDetails

from code_health import run, stop
from code_health.ratchet import CATEGORIES, CONFIGURED, COUNTS, ZERO
from code_health.rule_ledger import refuse_ledgered_pins, refuse_unknown_rules

CONFIG = ".code-health.json"
Count = Annotated[int, Field(strict=True, ge=0)]
DEFAULT_LIMITS = {
    "file_lines": 1000,
    "complexity": 25,
    "nesting": 4,
    "params": 5,
    "docstring_code": 0,
    "clone_pairs": 0,
}
ItemPins = dict[str, Count]
Limits = create_model(
    "Limits",
    __config__=ConfigDict(extra="forbid", strict=True),
    **{category: (Count, ...) for category in (*CONFIGURED, "clone_pairs")},
)
PIN_FIELDS = {
    **{
        category: (ItemPins, Field(default_factory=dict))
        for category in (*CONFIGURED, *ZERO)
    },
    **{category: (Count, 0) for category in COUNTS},
    "unused": (list[str], Field(default_factory=list)),
}
Pins = create_model(
    "Pins", __config__=ConfigDict(extra="forbid", strict=True), **PIN_FIELDS
)
CommittedPins = create_model(
    "CommittedPins", __config__=ConfigDict(extra="ignore", strict=True), **PIN_FIELDS
)


class Ceilings(BaseModel, extra="ignore", strict=True):
    """The committed config as a ceiling: a category this checker no longer
    measures bounds nothing and is not read."""

    limits: Limits
    pins: CommittedPins


class CodeHealthConfig(BaseModel, extra="forbid", strict=True):
    exclude: list[str]
    allowed: dict[str, list[str]]
    limits: Limits
    pins: Pins


def problem_text(problem: ErrorDetails) -> str:
    if problem["type"] == "missing" and problem["loc"] == ("allowed",):
        return 'field missing; add "allowed": {}'
    return problem["msg"]


def decode_config[Decoded: BaseModel](
    model: type[Decoded], text: str, source: str
) -> Decoded:
    try:
        return model.model_validate_json(text)
    except ValidationError as error:
        problems = [
            f"{source}.{'.'.join(map(str, problem['loc']))}: {problem_text(problem)}"
            for problem in error.errors(include_input=False, include_url=False)
        ]
        stop("\ncode-health: ".join(problems))


def committed_config(root: Path) -> Ceilings | None:
    head = run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], root, ok=(0, 1))
    tracked = (
        run(["git", "ls-tree", "--name-only", "HEAD", "--", CONFIG], root)
        if head
        else ""
    )
    if not tracked:
        return None
    return decode_config(
        Ceilings, run(["git", "show", f"HEAD:{CONFIG}"], root), f"HEAD:{CONFIG}"
    )


def enforce_ceilings(config: CodeHealthConfig, committed: Ceilings) -> None:
    ceilings = committed.pins.model_dump(exclude_unset=True)
    proposed = config.pins.model_dump(exclude_unset=True)
    for category, ceiling in ceilings.items():
        field = f"{CONFIG}.pins.{category}"
        if category not in proposed:
            stop(f"{field}: cannot remove a committed category")
        pin = proposed[category]
        if isinstance(ceiling, dict):
            raised = [
                key
                for key, count in pin.items()
                if key not in ceiling or count > ceiling[key]
            ]
        elif isinstance(ceiling, list):
            raised = sorted(set(pin) - set(ceiling))
        else:
            raised = [category] if pin > ceiling else []
        if raised:
            stop(f"{field}: exceeds HEAD ceiling: {', '.join(raised)}")
    limits = committed.limits.model_dump()
    for category, limit in config.limits.model_dump().items():
        if limit > limits[category]:
            stop(f"{CONFIG}.limits.{category}: exceeds HEAD limit")


def load_config(root: Path, baseline: bool) -> tuple[CodeHealthConfig, set[str]]:
    path = root / CONFIG
    if baseline and not path.exists():
        config = CodeHealthConfig(
            exclude=[],
            allowed={},
            limits=Limits(**DEFAULT_LIMITS),
            pins=Pins(),
        )
    else:
        try:
            config = decode_config(CodeHealthConfig, path.read_text(), CONFIG)
        except OSError as error:
            stop(f"{CONFIG}: {error.strerror}")
    refuse_ledgered_pins(CONFIG, config.pins.ast_grep)
    refuse_unknown_rules(CONFIG, sorted(config.allowed))
    missing = set(CATEGORIES) - config.pins.model_fields_set
    committed = committed_config(root)
    if committed is not None:
        enforce_ceilings(config, committed)
    if baseline and not missing:
        stop(f"{CONFIG}.pins: baseline already initialized; pins may only decrease")
    if missing and not baseline:
        stop(
            f"{CONFIG}.pins.{sorted(missing)[0]}: "
            "category not initialized; run --baseline"
        )
    return config, missing


def refuse_unmatched_allowances(
    allowed: dict[str, list[str]], files: list[str]
) -> None:
    for rule_id, globs in sorted(allowed.items()):
        for glob in globs:
            if not any(fnmatch.fnmatch(path, glob) for path in files):
                stop(f"{CONFIG}.allowed.{rule_id}: {glob} matches no measured file")
