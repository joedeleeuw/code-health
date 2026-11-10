"""The repository's classes and the contracts their resolved lineage states: a
base class that declares a method, a standard library base that declares it, a
Protocol, a subclass that overrides it. An outside base whose members the
engine cannot read leaves the lineage incomplete.
"""

import ast
import importlib
import sys
from collections import defaultdict
from functools import cache

from code_health.reaching import Reaching
from code_health.symbols import FUNCTIONS, Evaluation, Symbols
from code_health.visibility import assigned

PROTOCOLS = {"typing.Protocol", "typing_extensions.Protocol"}
ARITHMETIC = (
    "add sub mul matmul truediv floordiv mod divmod pow lshift rshift and xor or"
).split()
DATA_MODEL = frozenset(
    (
        "new init del repr str bytes format lt le eq ne gt ge hash bool getattr"
        " getattribute setattr delattr dir get set delete set_name init_subclass"
        " class_getitem call len length_hint getitem setitem delitem missing iter"
        " reversed contains neg pos abs invert complex int float index round"
        " trunc floor ceil enter exit aenter aexit await aiter anext buffer"
        " release_buffer instancecheck subclasscheck mro_entries prepare copy"
        " deepcopy reduce reduce_ex getstate setstate getnewargs getnewargs_ex"
        " fspath sizeof subclasshook"
    ).split()
    + ARITHMETIC
    + [f"r{name}" for name in ARITHMETIC]
    + [f"i{name}" for name in ARITHMETIC]
)


@cache
def standard_class(qualified: str) -> type | None:
    """The class a standard library qualified name names, such as
    builtins.dict or unittest.TestCase; None for any other name."""
    parts = qualified.split(".")
    if parts[0] not in sys.stdlib_module_names:
        return None
    for split in range(len(parts) - 1, 0, -1):
        try:
            found = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attribute in parts[split:]:
            found = getattr(found, attribute, None)
        return found if isinstance(found, type) else None
    return None


def members(node: ast.ClassDef) -> set[str]:
    found = set()
    for statement in node.body:
        if isinstance(statement, (*FUNCTIONS, ast.ClassDef)):
            found.add(statement.name)
        else:
            found.update(t.id for t in assigned(statement) if isinstance(t, ast.Name))
    return found


class Hierarchy:
    """The repository's classes and the lineage their bases give them. Contract
    evidence comes only from bases decided by a single reaching binding; a base
    that does not resolve, or resolves without one, leaves the lineage
    incomplete."""

    def __init__(self, trees: dict[str, ast.Module], symbols: Symbols):
        self.symbols = symbols
        self.reaching = Reaching(symbols)
        self.edges: dict[int, list[tuple[str, object, bool]]] = {}
        self.certain_lineage: dict[int, list[ast.ClassDef]] = {}
        self.heirs: dict[int, list[ast.ClassDef]] = defaultdict(list)
        self.possible_heirs: dict[int, list[ast.ClassDef]] = defaultdict(list)
        classes = [
            node
            for tree in trees.values()
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        ]
        for node in classes:
            certain = {id(a) for a in self.ancestors(node)}
            for ancestor in symbols.ancestors(node):
                target = self.heirs if id(ancestor) in certain else self.possible_heirs
                target[id(ancestor)].append(node)

    def bases(self, node: ast.ClassDef) -> list[tuple[str, object, bool]]:
        """Each base as (kind, class or outside name, decided), with ("unknown",
        None, False) for a base that does not resolve or resolves to something
        other than a class, such as the result of a class factory call."""
        if id(node) in self.edges:
            return self.edges[id(node)]
        defining = Evaluation(self.symbols, self.symbols.files[id(node)], node, {})
        found = []
        for base in node.bases:
            resolution = defining.resolve(
                base.value if isinstance(base, ast.Subscript) else base
            )
            decided = resolution is not None and self.reaching.certain(defining, base)
            if resolution is None or any(
                kind not in ("class", "outside") for kind, _ in resolution
            ):
                found.append(("unknown", None, False))
            found += [
                (kind, target, decided)
                for kind, target in resolution or []
                if kind in ("class", "outside")
            ]
        self.edges[id(node)] = found
        return found

    def ancestors(self, node: ast.ClassDef) -> list[ast.ClassDef]:
        """The repository classes a class inherits through decided bases."""
        if id(node) in self.certain_lineage:
            return self.certain_lineage[id(node)]
        found: list[ast.ClassDef] = []
        self.certain_lineage[id(node)] = found
        seen, pending = {id(node)}, [node]
        while pending:
            for kind, parent, decided in self.bases(pending.pop()):
                if kind == "class" and decided and id(parent) not in seen:
                    seen.add(id(parent))
                    found.append(parent)
                    pending.append(parent)
        return found

    def incomplete(self, node: ast.ClassDef, name: str) -> str:
        """Why a lineage fact about the method is unknown: a base in its
        lineage that is not decided, a class that may inherit it and defines the
        name, or an outside base whose members the engine cannot read; empty
        when every fact is decided."""
        for current in (node, *self.symbols.ancestors(node)):
            if any(not decided for _, _, decided in self.bases(current)):
                return f"a base of {current.name} has no single reaching binding"
        overriding = [
            heir for heir in self.possible_heirs[id(node)] if name in members(heir)
        ]
        if overriding:
            return f"{overriding[0].name} may inherit it and defines {name}"
        unread = [
            qualified
            for qualified in self.outside_bases(node)
            if qualified not in PROTOCOLS and standard_class(qualified) is None
        ]
        if unread:
            return f"the members of outside base {unread[0]} are not read"
        return ""

    def outside_bases(self, node: ast.ClassDef) -> list[str]:
        """The qualified names outside the repository that the decided bases of
        a class's decided lineage resolve to."""
        return [
            target
            for current in (node, *self.ancestors(node))
            for kind, target, decided in self.bases(current)
            if kind == "outside" and decided and target
        ]

    def contract(self, node: ast.ClassDef, name: str) -> str:
        """Why the class's decided hierarchy needs the method, empty when nothing
        does."""
        if any(name in members(parent) for parent in self.ancestors(node)):
            return "a base class declares it"
        outside = self.outside_bases(node)
        declaring = [standard_class(qualified) for qualified in outside]
        if any(kind is not None and hasattr(kind, name) for kind in declaring):
            return "a standard library base declares it"
        if any(qualified in PROTOCOLS for qualified in outside):
            return "a Protocol lists it"
        if any(name in members(heir) for heir in self.heirs[id(node)]):
            return "a subclass overrides it"
        return ""
