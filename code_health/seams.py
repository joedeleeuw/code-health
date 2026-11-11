"""Seam metrics from the codebase-design skill: a seam with one adapter, and an
injected dependency that no production caller overrides.

One adapter means a hypothetical seam. A seam is a Python class with an ABC
base, an ABCMeta metaclass or abstract methods and no base. Its adapters are
subclasses in any file, tests included. A seam does not count when its one
adapter is itself extended or sits in a test file. Python Protocols are
structural and are not counted. TypeScript single-adapter and test-only seams
are enforced by antidrift/no-hypothetical-seam.

A test-only seam is a parameter whose default is a dependency: a module-level
function of the repository, or a callable from the standard library's clock,
process, file, network and randomness modules. The function calls it or
__init__ stores it on self, and no call site outside tests passes it. Class
defaults, values such as a dtype and library functions such as an activation
are configuration, not seams. A call to a class is a call to its __init__.
"""

import ast
import re
from collections import defaultdict

from code_health.imports import Modules
from code_health.symbols import FUNCTIONS, Evaluation, Symbols, parents

TEST = re.compile(
    r"(^|/)(tests?|__tests__|e2e)/|(^|/)test_[^/]*\.py$|_test\.py$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$"
)
EFFECTS = frozenset(
    "time os subprocess random uuid datetime socket shutil requests httpx urllib"
    " asyncio sys".split()
)
EFFECT_BUILTINS = frozenset(("open", "input"))


def name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return name_of(node.value)
    return None


def is_abstract(node: ast.ClassDef) -> bool:
    bases = [name_of(base) for base in node.bases]
    meta = [name_of(k.value) for k in node.keywords if k.arg == "metaclass"]
    methods = [
        d for d in node.body if isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    marked = any(
        name_of(decorator) == "abstractmethod"
        for method in methods
        for decorator in method.decorator_list
    )
    return "ABC" in bases or "ABCMeta" in meta or (marked and not bases)


def python_single_adapters(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    seams: dict[str, tuple[str, int]] = {}
    subclasses: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if is_abstract(node) and not TEST.search(path):
                seams.setdefault(node.name, (path, node.lineno))
            for base in filter(None, map(name_of, node.bases)):
                subclasses[base].append((path, node.lineno, node.name))
    found: dict[str, list[str]] = {}
    for seam, (path, line) in sorted(seams.items()):
        adapters = subclasses.get(seam, [])
        # a test double as the one nominal adapter means production fills the seam
        # structurally: two adapters
        if len(adapters) == 1 and not subclasses.get(adapters[0][2]):
            where, at, adapter = adapters[0]
            if TEST.search(where):
                continue
            found.setdefault(path, []).append(
                f"line {line}: {seam} has one adapter, {adapter} at {where}:{at}"
            )
    return found


def defaults(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    tail = positional[len(positional) - len(arguments.defaults) :]
    named = dict(zip([a.arg for a in tail], arguments.defaults, strict=True))
    named |= {
        a.arg: d
        for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
        if d is not None
    }
    return named


def injected(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters the function calls, or that __init__ stores on self."""
    used = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    if function.name == "__init__":
        used |= {
            node.value.id
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and any(
                isinstance(t, ast.Attribute) and name_of(t.value) == "self"
                for t in node.targets
            )
        }
    return used


def is_dependency(default: ast.expr, first_party: set[str]) -> bool:
    """A repository function, or a callable from an effectful standard module."""
    if isinstance(default, ast.Name):
        return default.id in first_party or default.id in EFFECT_BUILTINS
    root = default
    while isinstance(root, ast.Attribute):
        root = root.value
    return isinstance(default, ast.Attribute) and name_of(root) in EFFECTS


def passes(call: ast.Call, parameter: str, index: int | None) -> bool:
    if any(keyword.arg == parameter for keyword in call.keywords):
        return True
    starred = any(isinstance(a, ast.Starred) for a in call.args)
    return index is not None and len(call.args) > index and not starred


def initializer(symbols: Symbols, cls: ast.ClassDef) -> ast.AST | None:
    """The __init__ a call to the class runs: its own or its nearest
    repository ancestor's."""
    for owner in (cls, *symbols.ancestors(cls)):
        for statement in owner.body:
            if isinstance(statement, FUNCTIONS) and statement.name == "__init__":
                return statement
    return None


def record_call(
    resolved_calls: dict[int, list[tuple[str, ast.Call]]],
    symbols: Symbols,
    path: str,
    call: ast.Call,
) -> None:
    targets = Evaluation(symbols, path, call).resolve(call.func)
    for kind, target in targets or []:
        if kind == "class" and isinstance(target, ast.ClassDef):
            target = initializer(symbols, target)
        if kind in ("function", "class") and target is not None:
            resolved_calls[id(target)].append((path, call))


def python_index(trees: dict[str, ast.Module], modules: Modules):
    """Calls by resolved target, production functions with their class, and the
    repository's module-level function names."""
    resolved_calls: dict[int, list[tuple[str, ast.Call]]] = defaultdict(list)
    functions, first_party = [], set()
    parent_of = {path: parents(tree) for path, tree in trees.items()}
    symbols = Symbols(trees, parent_of, modules)
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                record_call(resolved_calls, symbols, path, node)
        if TEST.search(path):
            continue
        for node in tree.body:
            if isinstance(node, FUNCTIONS):
                first_party.add(node.name)
            owned = node.body if isinstance(node, ast.ClassDef) else [node]
            owner = node.name if isinstance(node, ast.ClassDef) else None
            functions += [
                (path, owner, item) for item in owned if isinstance(item, FUNCTIONS)
            ]
    return resolved_calls, functions, first_party


def unused_injections(owner, function, resolved_calls, first_party) -> list[str]:
    """The function's injected dependencies that no production call passes."""
    positional = [a.arg for a in function.args.posonlyargs + function.args.args]
    offset = 1 if positional[:1] in (["self"], ["cls"]) else 0
    used = injected(function)
    label = f"{owner}.{function.name}" if owner else function.name
    target_calls = resolved_calls.get(id(function), [])
    found = []
    for parameter, default in defaults(function).items():
        if not is_dependency(default, first_party) or parameter not in used:
            continue
        index = (
            positional.index(parameter) - offset if parameter in positional else None
        )
        callers = [
            where for where, call in target_calls if passes(call, parameter, index)
        ]
        if not any(not TEST.search(where) for where in callers):
            found.append(
                f"line {function.lineno}: {label}({parameter}={ast.unparse(default)}):"
                f" no production call passes it, {len(callers)} test call(s) do"
            )
    return found


def test_only_seams(
    trees: dict[str, ast.Module], modules: Modules
) -> dict[str, list[str]]:
    resolved_calls, functions, first_party = python_index(trees, modules)
    found: dict[str, list[str]] = {}
    for path, owner, function in functions:
        if unused := unused_injections(owner, function, resolved_calls, first_party):
            found.setdefault(path, []).extend(unused)
    return found
