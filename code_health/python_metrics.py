"""Python metrics: size limits from the AST, classes that restate one
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
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from code_health import counted, keyed, stop
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


@dataclass
class FunctionMetrics:
    complexity: int
    nesting_depth: int


@dataclass
class AnalyzedFunction:
    file_path: str
    name: str
    start_line: int
    metrics: FunctionMetrics


def compute_nesting(scope_node: ast.AST) -> int:
    max_depth = 0

    def walk_stmts(stmts: list[ast.stmt], depth: int) -> None:
        for stmt in stmts:
            walk_stmt(stmt, depth)

    def walk_stmt(node: ast.stmt, depth: int) -> None:
        nonlocal max_depth
        if depth > max_depth:
            max_depth = depth

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return

        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            walk_stmts(node.body, depth + 1)
            walk_stmts(node.orelse, depth + 1)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            walk_stmts(node.body, depth + 1)
            return
        if isinstance(node, ast.If):
            walk_stmts(node.body, depth + 1)
            if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                walk_stmt(node.orelse[0], depth)
            else:
                walk_stmts(node.orelse, depth + 1)
            return
        if isinstance(node, ast.Try):
            walk_stmts(node.body, depth + 1)
            for handler in node.handlers:
                walk_stmts(handler.body, depth + 2)
            walk_stmts(node.orelse, depth + 1)
            walk_stmts(node.finalbody, depth + 1)
            return
        if isinstance(node, ast.Match):
            for case in node.cases:
                walk_stmts(case.body, depth + 2)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                walk_stmt(child, depth)

    body = getattr(scope_node, "body", [])
    if isinstance(body, list):
        walk_stmts(body, 0)
    return max_depth


def compute_complexity(scope_node: ast.AST) -> int:
    complexity = 1

    def count_comp(
        comp: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        nonlocal complexity
        for gen in comp.generators:
            complexity += 1
            complexity += len(gen.ifs)

    def walk_expr(expr: ast.expr, direct_statement: bool = False) -> None:
        if isinstance(
            expr, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            if direct_statement:
                count_comp(expr)
        for child in ast.iter_child_nodes(expr):
            if not isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.stmt)
            ):
                walk_expr(child, False)

    def walk_stmt(node: ast.AST) -> None:
        nonlocal complexity
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return

        if isinstance(
            node,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.ExceptHandler,
                ast.match_case,
            ),
        ):
            complexity += 1

        if isinstance(
            node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Return, ast.Expr)
        ):
            expression = getattr(node, "value", None)
            if expression is not None:
                walk_expr(expression, direct_statement=True)
            for child in ast.iter_child_nodes(node):
                if child is not expression and not isinstance(
                    child,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.stmt),
                ):
                    walk_expr(child, False)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.match_case)):
                    walk_stmt(child)
                elif not isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    walk_expr(child, False)

    body = getattr(scope_node, "body", [])
    if isinstance(body, list):
        for stmt in body:
            walk_stmt(stmt)

    return complexity


def analyze_functions(trees: dict[str, ast.Module]) -> list[AnalyzedFunction]:
    analyzed: list[AnalyzedFunction] = []
    for file_path, tree in trees.items():
        analyzed.append(
            AnalyzedFunction(
                file_path=file_path,
                name="<module>",
                start_line=1,
                metrics=FunctionMetrics(
                    complexity=compute_complexity(tree),
                    nesting_depth=compute_nesting(tree),
                ),
            )
        )

        def walk(node: ast.AST, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn_name = f"{prefix}.{child.name}" if prefix else child.name
                    analyzed.append(
                        AnalyzedFunction(
                            file_path=file_path,
                            name=fn_name,
                            start_line=child.lineno,
                            metrics=FunctionMetrics(
                                complexity=compute_complexity(child),
                                nesting_depth=compute_nesting(child),
                            ),
                        )
                    )
                    walk(child, fn_name)
                elif isinstance(child, ast.ClassDef):
                    cls_name = f"{prefix}.{child.name}" if prefix else child.name
                    walk(child, cls_name)
                else:
                    walk(child, prefix)

        walk(tree, "")
    return sorted(analyzed, key=lambda f: (f.file_path, f.start_line))


def python_trees(root: Path, files: list[str]) -> dict[str, ast.Module]:
    trees = {}
    for path in files:
        try:
            trees[path] = ast.parse((root / path).read_bytes(), path)
        except (SyntaxError, ValueError) as error:
            stop(f"cannot parse {path}: {error}")
    return trees


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
    functions = analyze_functions(trees)

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
    shipped = Distributions(projects.manifests, listed)
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
        "test_only_seams": test_only_seams(trees, modules),
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
