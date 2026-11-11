"""Code-health ratchet.

Python repositories get Homer's file length, complexity, nesting depth
and parameter limits, and Rust files the file length limit; Python repositories also get classes that restate one
instance shape, functions that duplicate one body, AST import cycles,
code that pyright's resolution shows nothing reads, parameters a function only
forwards to other calls,
single-call hops that inlining at every caller shows no contract needs, and
docstrings that carry code or a date. Every repository gets the code-health ast-grep
rules, dead repository paths in Python strings, JSON and YAML values and markdown code
spans, JSON objects whose dotted keys shadow the file's own leaves, seams with
one adapter, and injected dependencies that only tests override. Measured Nix
files get native Statix and deadnix findings, ratcheted by rule and file;
both commands must be available on PATH when those files exist. Rust lints
are not run here: presets/rust holds the Clippy settings and lint levels a Rust
workspace adopts in its own Cargo.toml and clippy.toml.

The repository's code-health config holds the limits and the pins. A value over
its limit needs a pin, and a pin is a ceiling that only goes down. A pin above its
value, or one whose item is gone or back under the limit, is lowered in place and
the check fails until the lowered config is staged. Initial adoption pins the
current offenders; an initialized baseline cannot be raised with --baseline.

The config's "allowed" maps ast-grep rule IDs to path globs; a finding of that
rule in a matching file is neither counted nor reported ({} when none). A key that
is not a catalog rule ID or is a retired one, and a glob matching no measured
file, stop the check.

It measures the files in the index, minus lockfiles, workflow files and the
config's exclude globs; untracked scratch never enters the measurement.

--dependency-shapes PYTHON skips the ratchet and reports each first-party class
whose shape a class exported by a dependency that pyproject.toml declares
already has, read from the environment of the PYTHON interpreter. Every declared
distribution that applies there must be installed.

Exit status: 0 when every value is within its limit or pin, 1 when one is not,
a pin was lowered or a class restates a dependency's class, 2 when the check
cannot run (an unreadable file, a failed tool, a missing interpreter or a
declared distribution the environment lacks). Each problem is one line on
stderr. Every incomplete hop, shape and import analysis note is printed on
stderr on every run, pinned or not; otherwise a passing run prints nothing.
"""

import argparse
import ast
import fnmatch
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import yaml
from code_health import ERROR_EXIT, counted, run, stop
from code_health.config import (
    CONFIG,
    CodeHealthConfig,
    load_config,
    refuse_unmatched_allowances,
)
from code_health.distribution import Distributions
from code_health.dependencies import dependency_trees, exported_shapes
from code_health.imports import Modules
from code_health.nix import nix_lints
from code_health.python_metrics import measure_python, python_trees
from code_health.projects import Projects
from code_health.ratchet import INCOMPLETE, ratchet
from code_health.references import measure_references
from code_health.restatements import ExportedShapes, FirstPartyShapes
from code_health.rule_ledger import SGCONFIG as RULES
from code_health.source_language import source_languages
from pydantic import AliasPath, BaseModel, Field, ValidationError

SKIPPED = ("*.lock", "*-lock.json", "*-lock.yaml", "*.lockb", "go.sum", "*/go.sum")
SKIPPED += (".github/workflows/*", CONFIG)


class RuleMatch(BaseModel):
    rule_id: str = Field(validation_alias="ruleId")
    file: str
    line: int = Field(validation_alias=AliasPath("range", "start", "line"))
    message: str


def ast_grep(
    root: Path, languages: dict[str, str | None], allowed: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Rule matches per rule and file, minus the files the config allows per rule."""
    if not languages:
        return {}
    with TemporaryDirectory(prefix="code-health-rules-") as directory:
        config_path = Path(directory) / "sgconfig.yml"
        config = yaml.safe_load(RULES.read_text())
        config["ruleDirs"] = [
            str(RULES.parent / folder) for folder in config["ruleDirs"]
        ]
        if "utilDirs" in config:
            config["utilDirs"] = [
                str(RULES.parent / folder) for folder in config["utilDirs"]
            ]
        config.pop("testConfigs", None)
        batches = (
            (
                None,
                [
                    p
                    for p, language in languages.items()
                    if language not in {"Python", "Bash"}
                ],
            ),
            (
                "Python",
                [p for p, language in languages.items() if language == "Python"],
            ),
            ("Bash", [p for p, language in languages.items() if language == "Bash"]),
        )
        matches = ""
        for language, paths in batches:
            if paths:
                config["languageGlobs"] = {language: ["*"]} if language else {}
                config_path.write_text(yaml.safe_dump(config))
                command = [
                    "ast-grep",
                    "scan",
                    "--config",
                    str(config_path),
                    "--json=stream",
                    *paths,
                ]
                matches += run(command, root, ok=(0, 1))
    found: dict[str, list[str]] = {}
    for number, line in enumerate(matches.splitlines(), 1):
        try:
            match = RuleMatch.model_validate_json(line, strict=True)
        except ValidationError as error:
            stop(f"invalid ast-grep output line {number}: {error}")
        globs = allowed.get(match.rule_id, [])
        if any(fnmatch.fnmatch(match.file, glob) for glob in globs):
            continue
        key = f"{match.rule_id}::{match.file}"
        found.setdefault(key, []).append(f"line {match.line + 1}: {match.message}")
    return found


class TrackedSources(NamedTuple):
    listed: list[str]
    files: list[str]
    languages: dict[str, str | None]
    trees: dict[str, ast.Module]


def sources(root: Path, exclude: list[str]) -> TrackedSources:
    """The listed files, the measured ones, their languages and the Python trees."""
    listed = run(["git", "ls-files", "-z"], root)
    listed = [path for path in listed.split("\0") if (root / path).is_file()]
    skipped = [*exclude, *SKIPPED]
    files = [p for p in listed if not any(fnmatch.fnmatch(p, g) for g in skipped)]
    languages = source_languages(root, files)
    trees = python_trees(
        root, [p for p, language in languages.items() if language == "Python"]
    )
    return TrackedSources(listed, files, languages, trees)


def measure(
    root: Path, tracked: TrackedSources, config: CodeHealthConfig
) -> tuple[dict, dict]:
    rules = ast_grep(root, tracked.languages, config.allowed)
    values, findings = measure_python(root, tracked.trees, tracked.listed)
    values |= {"ast_grep": counted(rules)}
    findings |= {"ast_grep": rules}
    counts, found = measure_references(
        root, tracked.files, tracked.listed, tracked.trees
    )
    merged_values = values | counts
    merged_findings = findings | found
    merged_values["file_lines"] = merged_values["file_lines"] | {
        path: len((root / path).read_bytes().splitlines())
        for path, language in tracked.languages.items()
        if language == "Rust"
    }
    return merged_values, merged_findings


def ratcheted(root: Path, baseline: bool) -> list[str]:
    config, initialize = load_config(root, baseline)
    tracked = sources(root, config.exclude)
    refuse_unmatched_allowances(config.allowed, tracked.files)
    values, findings = measure(root, tracked, config)
    nix_findings = nix_lints(root, tracked.files)
    values["nix_lints"] = counted(nix_findings)
    findings["nix_lints"] = nix_findings
    for category in INCOMPLETE:
        label = category.replace("_", " ")
        for path, notes in sorted(findings[category].items()):
            for note in notes:
                print(f"code-health: {label}: {path}: {note}", file=sys.stderr)
    document = config.model_dump(exclude_unset=True)
    pins, failures = ratchet(values, findings, document, initialize)
    if pins != document["pins"] or not (root / CONFIG).exists():
        document["pins"] = pins
        (root / CONFIG).write_text(json.dumps(document, indent=2) + "\n")
        if not baseline:
            failures.append(f"pins lowered in {CONFIG}; stage it with this change")
    return failures


def dependency_shapes(root: Path, python: str) -> list[str]:
    config, _ = load_config(root, baseline=False)
    tracked = sources(root, config.exclude)
    trees = tracked.trees
    projects = Projects(root, trees)
    installed, uncovered = dependency_trees(projects.manifests, python)
    dependency, exported, unreadable = exported_shapes(installed)
    shapes = FirstPartyShapes(
        root,
        Modules(trees, projects.source_roots, projects.aliases),
        projects,
        Distributions(projects.manifests, tracked.listed),
    )
    exporting = ExportedShapes(dependency, exported, shapes.index.typed)
    findings = [*uncovered, *unreadable, *shapes.dependency_clones(exporting)]
    return [f"dependency_shapes: {finding}" for finding in findings]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--baseline", action="store_true", help="initialize missing pin categories"
    )
    mode.add_argument(
        "--dependency-shapes",
        metavar="PYTHON",
        help="instead of the ratchet, report first-party classes that restate a "
        "class exported by a declared dependency installed for PYTHON",
    )
    arguments = parser.parse_args()
    root = Path(run(["git", "rev-parse", "--show-toplevel"], Path.cwd()).strip())
    try:
        if arguments.dependency_shapes:
            failures = dependency_shapes(root, arguments.dependency_shapes)
        else:
            failures = ratcheted(root, arguments.baseline)
    except OSError as error:
        print(f"code-health: {error.filename}: {error.strerror}", file=sys.stderr)
        sys.exit(ERROR_EXIT)
    for failure in failures:
        print(f"code-health: {failure}", file=sys.stderr)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
