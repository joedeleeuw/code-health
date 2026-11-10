"""Python metrics: size limits from pyscn and the AST, classes that restate one
instance shape from the AST, import-time cycles from the AST, unused code from
pyright's resolution of each name, imports that resolve in none of the
repository's environments, docstrings that carry code or a prose date,
single-adapter and test-only seams, and single-call hops that deleting and
inlining the function shows no contract needs. Shape and import analysis the
AST cannot settle are their own categories, per file, apart from the
restatements and cycles they might hide.
"""

import ast
import re
import shutil
import tokenize
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import AliasPath, BaseModel, Field, ValidationError

from code_health import counted, keyed, run, stop
from code_health.distribution import Distributions
from code_health.environments import Environments, environments
from code_health.hops import (
    decisions,
    incomplete_hop_analysis,
    single_call_hops,
)
from code_health.imports import Modules, import_cycles
from code_health.projects import Projects
from code_health.repository import Repository
from code_health.resolver import Resolver, Search
from code_health.restatements import FirstPartyShapes
from code_health.seams import python_single_adapters, test_only_seams
from code_health.unused import UnusedCode

PYSCN = ["uvx", "--from", "pyscn==1.32.4", "pyscn", "analyze", "--json"]
PYSCN += ["--output", "-", "--select", "complexity"]
DOCUMENTED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
DATE = re.compile(
    r"(?<![\w/'\"`<>.-])(?:(?:19|20)\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"|20\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?![\w/'\"`<>-]|\.\w)"
)
NONE = {
    "file_lines": {},
    "complexity": {},
    "nesting": {},
    "params": {},
    "docstring_code": {},
    "dated_docstrings": {},
    "single_call_hops": {},
    "unresolved_imports": {},
    "incomplete_hop_analysis": {},
    "incomplete_shape_analysis": {},
    "incomplete_import_analysis": {},
    "single_adapter_seams": {},
    "test_only_seams": {},
    "clone_pairs": 0,
    "cycles": 0,
    "unused": [],
}

NO_FINDINGS = {
    "docstring_code": {},
    "dated_docstrings": {},
    "single_call_hops": {},
    "unresolved_imports": {},
    "incomplete_hop_analysis": {},
    "incomplete_shape_analysis": {},
    "incomplete_import_analysis": {},
    "single_adapter_seams": {},
    "test_only_seams": {},
    "cycles": {"": []},
    "clone_pairs": {"": []},
}


class FunctionMetrics(BaseModel):
    complexity: int
    nesting_depth: int


class AnalyzedFunction(BaseModel):
    file_path: str
    name: str
    start_line: int
    metrics: FunctionMetrics


class PyscnReport(BaseModel):
    functions: list[AnalyzedFunction] = Field(
        validation_alias=AliasPath("complexity", "functions")
    )


def python_trees(root: Path, files: list[str]) -> dict[str, ast.Module]:
    trees = {}
    for path in files:
        try:
            trees[path] = ast.parse((root / path).read_bytes(), path)
        except (SyntaxError, ValueError) as error:
            stop(f"cannot parse {path}: {error}")
    return trees


def pyscn_report(root: Path, files: list[str]) -> PyscnReport:
    with TemporaryDirectory(prefix="code-health-python-") as directory:
        analysis_root = Path(directory)
        originals = {}
        for index, path in enumerate(files):
            analyzed = f"{index}/{Path(path).name}.py"
            originals[analyzed] = path
            destination = analysis_root / analyzed
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / path, destination)
        config = analysis_root / ".pyscn.toml"
        config.write_text(
            '[analysis]\ninclude_patterns = ["**/*.py"]\n'
            'exclude_patterns = ["**/.git/**"]\n'
        )
        output = run([*PYSCN, "--config", str(config), *originals], analysis_root)
        try:
            report = PyscnReport.model_validate_json(output, strict=True)
        except ValidationError as error:
            stop(f"invalid pyscn report: {error}")
    for function in report.functions:
        function.file_path = originals[function.file_path]
    return report


def parameter_rows(path: str, tree: ast.AST) -> list[tuple[int, str, int]]:
    rows = []
    for scope in ast.walk(tree):
        in_class = isinstance(scope, ast.ClassDef)
        body = getattr(scope, "body", None)
        for node in body if isinstance(body, list) else []:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            signature = node.args
            names = [
                argument.arg
                for argument in (
                    *signature.posonlyargs,
                    *signature.args,
                    *signature.kwonlyargs,
                )
            ]
            if in_class and names[:1] in (["self"], ["cls"]):
                names = names[1:]
            name = f"{scope.name}.{node.name}" if in_class else node.name
            rows.append((node.lineno, f"{path}::{name}", len(names)))
    return rows


def parameter_counts(trees: dict[str, ast.AST]) -> dict[str, int]:
    rows = []
    for path, tree in trees.items():
        rows += parameter_rows(path, tree)
    return keyed([(key, count) for _, key, count in sorted(rows)])


def code_marker(text: str) -> str:
    """Inline code, doctest prompts and reST literal blocks are code."""
    for marker in ("`", ">>>"):
        if marker in text:
            return marker
    if text.rstrip().endswith("::") or "::\n" in text:
        return "::"
    return ""


def first_date(text: str) -> str:
    found = DATE.search(text)
    return found.group() if found else ""


def docstrings(
    trees: dict[str, ast.AST], hit: Callable[[str], str]
) -> dict[str, list[str]]:
    """Per file, the docstrings in which the hit function finds something."""
    found: dict[str, list[str]] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            text = ast.get_docstring(node) if isinstance(node, DOCUMENTED) else ""
            if text and (what := hit(text)):
                line = node.body[0].lineno
                found.setdefault(path, []).append(f"line {line}: {what}")
    return found


def python_sources(root: Path, paths: list[str]) -> dict[str, str]:
    """Each file's text, decoded as Python reads it."""
    sources = {}
    for path in paths:
        with tokenize.open(root / path) as source:
            sources[path] = source.read()
    return sources


def search(root: Path, modules: Modules, installed: Environments) -> Search:
    """Where the resolver looks for an import, the same for every file: the
    declared source roots, the directory above each package, each directory a
    file puts on the import path, then the packages the repository's
    environments install. A script's own directory is left to pyright, which
    searches it for that script alone. The directories are named as the
    resolver names the checkout, through no symbolic link."""
    checkout = root.resolve()
    directories = [str(checkout / folder) for folder in modules.search_roots()]
    return Search(installed.interpreter, [*directories, *installed.packages])


def measure_python(
    root: Path, trees: dict[str, ast.Module], listed: list[str]
) -> tuple[dict, dict]:
    """The Python values, and the findings behind the counted ones; listed is
    every path the repository tracks, where a use of measured code counts."""
    if not trees:
        return NONE, NO_FINDINGS
    files = list(trees)
    report = pyscn_report(root, files)
    functions = sorted(
        report.functions,
        key=lambda function: (function.file_path, function.start_line),
    )

    def per_function(metric: Callable[[FunctionMetrics], int]) -> dict[str, int]:
        return keyed(
            [
                (
                    f"{function.file_path}::{function.name}",
                    metric(function.metrics),
                )
                for function in functions
            ]
        )

    projects = Projects(root, trees)
    modules = Modules(trees, projects.source_roots, projects.aliases)
    graph = import_cycles(modules)
    shipped = Distributions(root, files, listed)
    shapes = FirstPartyShapes(root, modules, projects, shipped)
    restated = shapes.clone_groups()
    decided = decisions(Repository(trees, shipped, modules))
    sources = python_sources(root, files)
    where = search(root, modules, environments(root, listed, sources))
    with Resolver(root, sources, trees, where) as resolver:
        unresolved = resolver.unresolved_imports(trees)
        measured = UnusedCode(resolver, trees, set(listed), shapes.specimens)
        unused = measured.findings()
    findings = {
        "docstring_code": docstrings(trees, code_marker),
        "dated_docstrings": docstrings(trees, first_date),
        "single_call_hops": single_call_hops(decided),
        "unresolved_imports": unresolved,
        "incomplete_hop_analysis": incomplete_hop_analysis(decided),
        "incomplete_shape_analysis": restated.incomplete,
        "incomplete_import_analysis": graph.incomplete,
        "single_adapter_seams": python_single_adapters(trees),
        "test_only_seams": test_only_seams(trees),
        "cycles": {"": graph.cycles},
        "clone_pairs": {"": restated.findings},
    }
    measured = {
        "file_lines": {p: len((root / p).read_bytes().splitlines()) for p in files},
        "complexity": per_function(lambda metrics: metrics.complexity),
        "nesting": per_function(lambda metrics: metrics.nesting_depth),
        "params": parameter_counts(trees),
        "docstring_code": counted(findings["docstring_code"]),
        "dated_docstrings": counted(findings["dated_docstrings"]),
        "single_call_hops": counted(findings["single_call_hops"]),
        "unresolved_imports": counted(unresolved),
        "incomplete_hop_analysis": counted(findings["incomplete_hop_analysis"]),
        "incomplete_shape_analysis": counted(restated.incomplete),
        "incomplete_import_analysis": counted(graph.incomplete),
        "single_adapter_seams": counted(findings["single_adapter_seams"]),
        "test_only_seams": counted(findings["test_only_seams"]),
        "clone_pairs": len(restated.findings),
        "cycles": len(graph.cycles),
        "unused": unused,
    }
    return measured, findings
