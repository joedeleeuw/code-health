"""Code-health checks.

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
files get native Statix and deadnix findings, by rule and file;
both commands must be available on PATH when those files exist. Rust lints
are not run here: presets/rust holds the Clippy settings and the cargo alias a Rust
workspace adopts in its own clippy.toml and .cargo/config.toml.

Settings are the [tool.code-health] table of the repository's pyproject.toml:
exclude globs, the size limits, and "allowed". Every finding fails. "allowed"
maps a check name or a rule ID to path globs; a finding of that check in a
matching file is not reported. A key that is neither a check nor a catalog rule
ID, or is a retired one, and a glob matching no measured file, stop the check.
A repository without the file or the table gets the defaults. The checker
writes nothing.

It measures the files in the index, minus lockfiles, workflow files and the
exclude globs; untracked scratch never enters the measurement.

--dependency-shapes PYTHON skips the checks and reports each first-party class
whose shape a class exported by a dependency that pyproject.toml declares
already has, read from the environment of the PYTHON interpreter. Every declared
distribution that applies there must be installed.

A Python, JSON or YAML file that does not parse is itself a finding
("unparsable") and is left out of the checks that need its contents; every
other check still runs.

Exit status: 0 when nothing is found outside the allowed files, 1 when a
finding is or a class restates a dependency's class, 2 when the check
cannot run (an unreadable file, a failed tool, a missing interpreter or a
declared distribution the environment lacks). Each problem is one line on
stderr. Every incomplete hop, shape and import analysis note is printed on
stderr on every run, allowed or not; otherwise a passing run prints nothing.
"""

import argparse
import ast
import fnmatch
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import yaml
from code_health import ERROR_EXIT, counted, run, stop
from code_health.checks import INCOMPLETE, failures
from code_health.config import load_config, refuse_unmatched_allowances
from code_health.distribution import Distributions
from code_health.dependencies import dependency_trees, exported_shapes
from code_health.imports import Modules
from code_health.nix import nix_lints
from code_health.python_metrics import measure_python, python_trees
from code_health.projects import Projects
from code_health.references import measure_references
from code_health.restatements import ExportedShapes, FirstPartyShapes
from code_health.rule_ledger import SGCONFIG as RULES
from code_health.source_language import source_languages
from pydantic import AliasPath, BaseModel, Field, ValidationError

SKIPPED = ("*.lock", "*-lock.json", "*-lock.yaml", "*.lockb", "go.sum", "*/go.sum")
SKIPPED += (".github/workflows/*",)


class RuleMatch(BaseModel):
    rule_id: str = Field(validation_alias="ruleId")
    file: str
    line: int = Field(validation_alias=AliasPath("range", "start", "line"))
    message: str


def ast_grep(root: Path, languages: dict[str, str | None]) -> dict[str, list[str]]:
    """Rule matches per rule and file."""
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
        key = f"{match.rule_id}::{match.file}"
        found.setdefault(key, []).append(f"line {match.line + 1}: {match.message}")
    return found


class TrackedSources(NamedTuple):
    listed: list[str]
    files: list[str]
    languages: dict[str, str | None]
    trees: dict[str, ast.Module]
    unparsable: dict[str, list[str]]


def sources(root: Path, exclude: list[str]) -> TrackedSources:
    """The listed files, the measured ones, their languages and the Python trees."""
    listed = run(["git", "ls-files", "-z"], root)
    listed = [path for path in listed.split("\0") if (root / path).is_file()]
    skipped = [*exclude, *SKIPPED]
    files = [p for p in listed if not any(fnmatch.fnmatch(p, g) for g in skipped)]
    languages = source_languages(root, files)
    trees, unparsable = python_trees(
        root, [p for p, language in languages.items() if language == "Python"]
    )
    return TrackedSources(listed, files, languages, trees, unparsable)


def measure(root: Path, tracked: TrackedSources) -> tuple[dict, dict]:
    rules = ast_grep(root, tracked.languages)
    values, findings = measure_python(root, tracked.trees, tracked.listed)
    values |= {"ast_grep": counted(rules)}
    findings |= {"ast_grep": rules}
    counts, found = measure_references(
        root, tracked.files, tracked.listed, tracked.trees
    )
    merged_values = values | counts
    merged_findings = findings | found
    merged_findings["unparsable"] = found["unparsable"] | tracked.unparsable
    merged_values["unparsable"] = counted(merged_findings["unparsable"])
    merged_values["file_lines"] = merged_values["file_lines"] | {
        path: len((root / path).read_bytes().splitlines())
        for path, language in tracked.languages.items()
        if language == "Rust"
    }
    return merged_values, merged_findings


def checked(root: Path) -> list[str]:
    config = load_config(root)
    tracked = sources(root, config.exclude)
    refuse_unmatched_allowances(config.allowed, tracked.files)
    values, findings = measure(root, tracked)
    nix_findings = nix_lints(root, tracked.files)
    values["nix_lints"] = counted(nix_findings)
    findings["nix_lints"] = nix_findings
    for category in INCOMPLETE:
        label = category.replace("_", " ")
        for path, notes in sorted(findings[category].items()):
            for note in notes:
                print(f"code-health: {label}: {path}: {note}", file=sys.stderr)
    return failures(values, findings, config.limits.model_dump(), config.allowed)


def dependency_shapes(root: Path, python: str) -> list[str]:
    config = load_config(root)
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
    parser.add_argument(
        "--dependency-shapes",
        metavar="PYTHON",
        help="instead of the checks, report first-party classes that restate a "
        "class exported by a declared dependency installed for PYTHON",
    )
    arguments = parser.parse_args()
    root = Path(run(["git", "rev-parse", "--show-toplevel"], Path.cwd()).strip())
    try:
        if arguments.dependency_shapes:
            failed = dependency_shapes(root, arguments.dependency_shapes)
        else:
            failed = checked(root)
    except OSError as error:
        print(f"code-health: {error.filename}: {error.strerror}", file=sys.stderr)
        sys.exit(ERROR_EXIT)
    for failure in failed:
        print(f"code-health: {failure}", file=sys.stderr)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
