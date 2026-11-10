"""The finite string values an expression takes, for the paths a module hands
to a type checker and the module names it imports by name at run time.

A string constant is its own value. An f-string folds when each of its parts
folds, a list, tuple or set display when each element does, and a / or +
between two folding values joins them as a path or concatenates them. A name
folds to the one value the scope binding it assigns it, or to the literal
values a pytest.mark.parametrize decorator on the enclosing function supplies
for it, through pytest.param and the rows of a list bound once. __file__ is
the module's own path under the repository root, and .parent above a folding
path is its directory. str(), os.fspath() and pathlib's path classes pass a
folding value through; os.path.join, a path class with several arguments and
.joinpath join them. Anything else does not fold, and one part that does not
fold leaves the whole expression unfolded. Each expression is read in the
scope that encloses it.
"""

import ast
import posixpath
from itertools import product

from code_health.bindings import ModuleUses, enclosing_scope
from code_health.imports import FUNCTIONS, loaded_statements
from code_health.scopes import parameter_names

ROOT = "⟨root⟩"
PARAMETER = object()
PATH_MAKERS = frozenset(
    {
        "builtins.str",
        "os.fspath",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PosixPath",
        "pathlib.PurePosixPath",
    }
)
JOINERS = frozenset({"os.path.join", "posixpath.join"})
PARAMETRIZE = frozenset({"pytest.mark.parametrize", "_pytest.mark.parametrize"})
PARAMETER_SETS = frozenset({"pytest.param", "_pytest.mark.param"})


def template_prefix(templates: set[str] | None, marker: str) -> str:
    """The literal text before the first substitution of one template."""
    if templates is None or len(templates) != 1:
        return ""
    return next(iter(templates)).split(marker, 1)[0]


def joined(parts: list[set[str]]) -> set[str]:
    return {posixpath.normpath(posixpath.join(*pieces)) for pieces in product(*parts)}


def argument_names(node: ast.expr) -> list[str] | None:
    """The parameter names a parametrize decorator declares: one comma-separated
    string or a list of strings."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [name.strip() for name in node.value.split(",")]
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(name, ast.Constant) and isinstance(name.value, str)
        for name in node.elts
    ):
        return [name.value for name in node.elts]
    return None


def assigned_to(name: str, statements) -> list[ast.expr]:
    """The values the statements assign to the name."""
    return [
        statement.value
        for statement in statements
        if isinstance(statement, (ast.Assign, ast.AnnAssign))
        and statement.value is not None
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in getattr(statement, "targets", None) or [statement.target]
        )
    ]


class StringValues:
    """The finite string values of one module's expressions."""

    def __init__(self, module: ModuleUses, tree: ast.Module):
        self.module = module
        self.loaded = list(loaded_statements(tree, (*FUNCTIONS, ast.ClassDef)))
        self.folding: set[tuple[int, str]] = set()

    def strings(self, expression: ast.expr) -> set[str] | None:
        if isinstance(expression, ast.Constant):
            return {expression.value} if isinstance(expression.value, str) else None
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            return self.union(expression.elts)
        if isinstance(expression, ast.JoinedStr):
            return self.formatted(expression)
        if isinstance(expression, ast.BinOp) and isinstance(
            expression.op, (ast.Div, ast.Add)
        ):
            sides = [self.strings(expression.left), self.strings(expression.right)]
            if None in sides:
                return None
            if isinstance(expression.op, ast.Div):
                return joined(sides)
            return {left + right for left, right in product(*sides)}
        if isinstance(expression, ast.Name):
            return self.named(expression)
        if isinstance(expression, ast.Attribute) and expression.attr == "parent":
            below = self.strings(expression.value)
            return None if below is None else {posixpath.dirname(p) for p in below}
        if isinstance(expression, ast.Call):
            return self.called(expression)
        return None

    def union(self, elements: list[ast.expr]) -> set[str] | None:
        found: set[str] = set()
        for element in elements:
            folded = self.strings(element)
            if folded is None:
                return None
            found |= folded
        return found

    def formatted(self, node: ast.JoinedStr) -> set[str] | None:
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion != -1:
                    return None
                part = part.value
            folded = self.strings(part)
            if folded is None:
                return None
            parts.append(folded)
        return {"".join(pieces) for pieces in product(*parts)}

    def named(self, name: ast.Name) -> set[str] | None:
        if name.id == "__file__":
            return {f"{ROOT}/{self.module.path}"}
        assigned, binding = self.binding(name.id, name)
        if assigned is None:
            return None
        if assigned is PARAMETER:
            return self.parametrized(binding, name.id)
        if (id(binding), name.id) in self.folding:
            return None
        self.folding.add((id(binding), name.id))
        try:
            return self.strings(assigned)
        finally:
            self.folding.discard((id(binding), name.id))

    def binding(self, name: str, use: ast.expr) -> tuple:
        """The one value the scope binding the name, as the use reads it,
        assigns it, with that scope; PARAMETER for a parameter of a function,
        None for several bindings or a class body."""
        scope = enclosing_scope(use, self.module.parent_of)
        binding = self.module.binding_scope(name, scope)
        if isinstance(binding, ast.Module):
            assigned = assigned_to(name, self.loaded)
        elif isinstance(binding, FUNCTIONS[:2]):
            if name in parameter_names(binding):
                return PARAMETER, binding
            assigned = assigned_to(name, ast.walk(binding))
        else:
            return None, binding
        return (assigned[0] if len(assigned) == 1 else None), binding

    def parametrized(self, function: ast.AST, name: str) -> set[str] | None:
        """The literal values a parametrize decorator on the function supplies
        for one of its parameters."""
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call) and len(decorator.args) >= 2):
                continue
            if self.module.held(decorator.func)[1] not in PARAMETRIZE:
                continue
            names = argument_names(decorator.args[0])
            if names is None or name not in names:
                continue
            rows = self.rows(decorator.args[1])
            if rows is None or any(len(row) != len(names) for row in rows):
                return None
            return self.union([row[names.index(name)] for row in rows])
        return None

    def rows(self, node: ast.expr) -> list[list[ast.expr]] | None:
        """The parameter sets a parametrize decorator's values display holds,
        each as its expressions."""
        if isinstance(node, ast.Name):
            assigned, _ = self.binding(node.id, node)
            if assigned is None or assigned is PARAMETER:
                return None
            return self.rows(assigned)
        if not isinstance(node, (ast.List, ast.Tuple)):
            return None
        found: list[list[ast.expr]] = []
        for element in node.elts:
            if isinstance(element, ast.Starred):
                spread = self.rows(element.value)
                if spread is None:
                    return None
                found += spread
            elif isinstance(element, (ast.List, ast.Tuple)):
                found.append(list(element.elts))
            elif (
                isinstance(element, ast.Call)
                and self.module.held(element.func)[1] in PARAMETER_SETS
            ):
                found.append(list(element.args))
            else:
                found.append([element])
        return found

    def called(self, call: ast.Call) -> set[str] | None:
        if call.keywords:
            return None
        dotted = self.module.held(call.func)[1]
        if dotted in PATH_MAKERS and len(call.args) == 1:
            return self.strings(call.args[0])
        parts: list[ast.expr] = []
        if dotted in PATH_MAKERS or dotted in JOINERS:
            parts = call.args
        elif isinstance(call.func, ast.Attribute) and call.func.attr == "joinpath":
            parts = [call.func.value, *call.args]
        if not parts:
            return None
        folded = [self.strings(part) for part in parts]
        return None if None in folded else joined(folded)

    def prefix(self, expression: ast.expr) -> str:
        """The one literal text an expression that does not fold starts with:
        the leading parts of an f-string or concatenation that fold to one
        value, followed through the name bound to it."""
        if isinstance(expression, ast.Name):
            assigned, _ = self.binding(expression.id, expression)
            if assigned is None or assigned is PARAMETER:
                return ""
            return self.prefix(assigned)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            left = self.strings(expression.left)
            if left is None or len(left) != 1:
                return self.prefix(expression.left)
            return left.pop() + self.prefix(expression.right)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Mod):
            return template_prefix(self.strings(expression.left), "%")
        if (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "format"
        ):
            return template_prefix(self.strings(expression.func.value), "{")
        if not isinstance(expression, ast.JoinedStr):
            return ""
        found = ""
        for part in expression.values:
            folded = self.strings(part)
            if folded is None or len(folded) != 1:
                break
            found += folded.pop()
        return found
