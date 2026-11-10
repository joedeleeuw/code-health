"""Where Python files spell each name, and what each place does with it."""

import ast
import re

from code_health.forwarding import documented
from code_health.resolver import Site, expression_site
from code_health.symbols import FUNCTIONS
from code_health.visibility import assigned, listed_strings

ACCESSORS = frozenset({"setter", "getter", "deleter"})
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DOCUMENTED = (ast.Module, ast.ClassDef, *FUNCTIONS)
COMPUTED_READERS = frozenset({"getattr", "hasattr"})


def assigned_sites(path: str, tree: ast.Module) -> set[Site]:
    """Every place the module assigns or deletes a name or an attribute."""
    return {
        expression_site(path, node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
        and not isinstance(node.ctx, ast.Load)
    }


def augmented_sites(path: str, tree: ast.Module) -> set[Site]:
    """Every name or attribute an augmented assignment reads and assigns."""
    return {
        expression_site(path, node.target)
        for node in ast.walk(tree)
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, (ast.Name, ast.Attribute))
    }


def accessor(decorator: ast.expr, name: str) -> bool:
    """Whether a decorator is a property's setter, getter or deleter on a
    definition of the property's own name, which it returns to that name."""
    return (
        isinstance(decorator, ast.Attribute)
        and decorator.attr in ACCESSORS
        and isinstance(decorator.value, ast.Name)
        and decorator.value.id == name
    )


class Occurrences:
    """Where the files spell each name, and what each place does. named holds
    each name some place reads as an attribute of anything, or a string
    outside a docstring spells as a word. A call spelled getattr or hasattr
    whose name is not a literal reads a name built at run time: computed holds
    each name a string of the same top-level statement is, the list such a
    loop reads from; prefixed holds the call with the literal start of a
    formatted name."""

    def __init__(self, trees: dict[str, ast.Module]):
        self.spelled: dict[str, list[Site]] = {}
        self.imports: dict[str, list[Site]] = {}
        self.reads: set[Site] = set()
        self.attributes: set[Site] = set()
        self.writes: set[Site] = set()
        self.augmented: set[Site] = set()
        self.patterned: set[str] = set()
        self.literals: dict[str, list[tuple[str, ast.Call, int]]] = {}
        self.strings: list[tuple[str, ast.Constant]] = []
        self.subclass_reads: list[tuple[str, ast.Attribute]] = []
        self.accessors: set[int] = set()
        self.named: set[str] = set()
        self.described: set[int] = set()
        self.computed: set[str] = set()
        self.prefixed: list[tuple[str, ast.Call, str]] = []
        recorders = {
            ast.Name: self.expression,
            ast.Attribute: self.expression,
            ast.FunctionDef: self.definition,
            ast.AsyncFunctionDef: self.definition,
            ast.Assign: self.listing,
            ast.AnnAssign: self.listing,
            ast.ImportFrom: self.imported,
            ast.keyword: self.keyword,
            ast.MatchClass: self.pattern,
            ast.Call: self.call,
            ast.Constant: self.constant,
        }
        for path, tree in trees.items():
            self.augmented |= augmented_sites(path, tree)
            self.writes |= assigned_sites(path, tree) - self.augmented
            self.described |= {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, DOCUMENTED) and node.body and documented(node)
            }
            for node in ast.walk(tree):
                if recorder := recorders.get(type(node)):
                    recorder(path, node)
            for statement in tree.body:
                self.computed_reads(path, statement)

    def computed_reads(self, path: str, statement: ast.stmt) -> None:
        inside = list(ast.walk(statement))
        calls = [
            node
            for node in inside
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") in COMPUTED_READERS
            and len(node.args) > 1
            and not isinstance(node.args[1], ast.Constant)
        ]
        if not calls:
            return
        self.computed.update(
            node.value
            for node in inside
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.isidentifier()
            and id(node) not in self.described
        )
        for call in calls:
            formatted = call.args[1]
            start = formatted.values[:1] if isinstance(formatted, ast.JoinedStr) else []
            for literal in start:
                if isinstance(literal, ast.Constant) and literal.value:
                    self.prefixed.append((path, call, literal.value))

    def definition(self, _path: str, node) -> None:
        """The name an accessor decorator spells is the property it completes,
        not a read of it."""
        self.accessors.update(
            id(decorator.value)
            for decorator in node.decorator_list
            if accessor(decorator, node.name)
        )

    def listing(self, _path: str, node: ast.Assign | ast.AnnAssign) -> None:
        if any(
            getattr(target, "id", "") == "__match_args__" for target in assigned(node)
        ):
            self.patterned.update(listed_strings(node.value) or [])

    def expression(self, path: str, node: ast.Name | ast.Attribute) -> None:
        if id(node) in self.accessors:
            return
        name = node.id if isinstance(node, ast.Name) else node.attr
        if name == "__subclasses__" and isinstance(node, ast.Attribute):
            self.subclass_reads.append((path, node))
        site = expression_site(path, node)
        self.spelled.setdefault(name, []).append(site)
        if site not in self.writes:
            self.reads.add(site)
            if isinstance(node, ast.Attribute):
                self.attributes.add(site)
                self.named.add(name)

    def imported(self, path: str, node: ast.ImportFrom) -> None:
        for alias in node.names:
            site = Site(path, alias.lineno, alias.col_offset)
            self.spelled.setdefault(alias.name, []).append(site)
            self.imports.setdefault(alias.name, []).append(site)
            self.reads.add(site)

    def keyword(self, path: str, node: ast.keyword) -> None:
        if node.arg:
            site = Site(path, node.lineno, node.col_offset)
            self.spelled.setdefault(node.arg, []).append(site)

    def pattern(self, _path: str, node: ast.MatchClass) -> None:
        self.patterned.update(node.kwd_attrs)

    def constant(self, path: str, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.append((path, node))
            if id(node) not in self.described:
                self.named.update(IDENTIFIER.findall(node.value))

    def call(self, path: str, node: ast.Call) -> None:
        for index, argument in enumerate(node.args):
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                for part in argument.value.split("."):
                    self.literals.setdefault(part, []).append((path, node, index))
