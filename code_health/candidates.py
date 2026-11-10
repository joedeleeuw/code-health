"""The declarations a module holds that may be unused."""

import ast
from dataclasses import dataclass

from code_health.resolver import Resolver, Site, expression_site, imported_modules
from code_health.symbols import FUNCTIONS
from code_health.visibility import assigned

LOCAL = (*FUNCTIONS, ast.Lambda)


@dataclass(frozen=True)
class Candidate:
    """A declaration that may be unused. The scope of a definition or variable
    is the module, class or function whose body binds it, and its owner that
    scope when it is a class; a field is a variable a class body annotates. An
    attribute has no scope, and its owner is the nearest class around the
    assignment."""

    kind: str
    name: str
    site: Site
    node: ast.AST
    owner: ast.ClassDef | None
    scope: ast.AST | None


def flattened(targets: list[ast.expr]) -> list[ast.expr]:
    found = []
    for target in targets:
        if isinstance(target, (ast.Tuple, ast.List)):
            found += flattened(target.elts)
        elif isinstance(target, ast.Starred):
            found += flattened([target.value])
        else:
            found.append(target)
    return found


def defined_in(scope: ast.AST) -> list:
    """Each function the body of the class or module defines, on a branch of
    it or not."""
    found, pending = [], list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, FUNCTIONS):
            found.append(node)
        elif not isinstance(node, (ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return found


class Declared:
    """The candidates one module declares, in source order; the class and name
    of each slot, the first parameter of each method that is not static, and
    the value of each variable that only names another."""

    def __init__(self, path: str, tree: ast.Module, resolver: Resolver):
        self.path = path
        self.resolver = resolver
        self.found: list[Candidate] = []
        self.imports: dict[tuple[int, str], list[Site]] = {}
        self.slots: dict[Site, tuple[ast.ClassDef, str]] = {}
        self.receivers: set[Site] = set()
        self.aliases: dict[Site, ast.expr] = {}
        self.collect(tree, tree, None)

    def definition(self, node, scope: ast.AST) -> None:
        owner = scope if isinstance(scope, ast.ClassDef) else None
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        kind = "method" if owner and kind == "function" else kind
        site = self.resolver.definition_site(self.path, node)
        self.found.append(Candidate(kind, node.name, site, node, owner, scope))
        static = any(
            getattr(decorator, "id", "") == "staticmethod"
            for decorator in node.decorator_list
        )
        if kind == "method" and not static:
            self.receivers.update(
                Site(self.path, first.lineno, first.col_offset)
                for first in [*node.args.posonlyargs, *node.args.args][:1]
            )

    def targets(self, statement: ast.stmt, scope: ast.AST, around) -> None:
        for alias in statement.names if isinstance(statement, ast.ImportFrom) else []:
            key = (id(scope), alias.asname or alias.name)
            site = Site(self.path, alias.lineno, alias.col_offset)
            self.imports.setdefault(key, []).append(site)
        if isinstance(statement, ast.Import):
            placed = imported_modules(statement, self.resolver.lines[self.path])
            for alias, (line, column, _) in zip(statement.names, placed, strict=True):
                site = Site(self.path, alias.lineno, alias.col_offset)
                site = Site(self.path, line, column) if alias.asname else site
                key = (id(scope), alias.asname or alias.name.split(".")[0])
                self.imports.setdefault(key, []).append(site)
        for target in flattened(assigned(statement)):
            site = expression_site(self.path, target)
            if (
                isinstance(scope, ast.ClassDef)
                and getattr(target, "id", "") == "__slots__"
            ):
                self.slots |= {
                    Site(self.path, listed.lineno, listed.col_offset): (
                        scope,
                        listed.value,
                    )
                    for listed in ast.walk(statement)
                    if isinstance(listed, ast.Constant)
                    and isinstance(listed.value, str)
                }
            if isinstance(target, ast.Attribute):
                self.found.append(
                    Candidate("attribute", target.attr, site, target, around, None)
                )
            elif isinstance(target, ast.Name) and not isinstance(scope, LOCAL):
                owner = scope if isinstance(scope, ast.ClassDef) else None
                annotated = owner and isinstance(statement, ast.AnnAssign)
                kind = "field" if annotated else "variable"
                self.found.append(
                    Candidate(kind, target.id, site, target, owner, scope)
                )
                named = getattr(statement, "value", None)
                direct = any(target is whole for whole in assigned(statement))
                if direct and isinstance(named, (ast.Name, ast.Attribute)):
                    self.aliases[site] = named

    def collect(self, node: ast.AST, scope: ast.AST, around) -> None:
        """Walks a scope, the module, class or function whose body this is;
        around is the nearest class enclosing it."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                self.definition(child, scope)
                self.collect(child, child, child)
            elif isinstance(child, FUNCTIONS):
                self.definition(child, scope)
                self.collect(child, child, around)
            elif isinstance(child, ast.Lambda):
                self.collect(child, child, around)
            else:
                if isinstance(child, ast.stmt):
                    self.targets(child, scope, around)
                self.collect(child, scope, around)
