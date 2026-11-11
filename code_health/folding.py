"""The finite string values an expression takes, for the paths a module puts
on sys.path or hands to a type checker and the module names it imports by
name at run time.

A string constant is its own value, and so is a constant string expression
that folded() evaluates. An f-string folds when each of its parts folds, a
list, tuple or set display when each element does, and a / or + between two
folding values joins them as a path or concatenates them. A name folds to the
one value its binding assigns it: ModuleStrings reads the module's loaded
statements, StringValues the scope that binds the name, where a parameter also
folds to the literal values a pytest.mark.parametrize decorator on the
enclosing function supplies for it, through pytest.param and the rows of a list
bound once. __file__ is the module's own path under the repository root,
.parent and .parents[n] above a folding path are its directories, and so is
os.path.dirname() of it. str(), os.fspath(), os.path.abspath(), realpath(),
normpath(), .resolve(), .absolute() and pathlib's path classes pass a folding
value through; os.path.join, a path class with several arguments and .joinpath
join them. ModuleStrings reads a callee as it is spelled, StringValues through
the imports that bind it. Anything else does not fold, and one part that does
not fold leaves the whole expression unfolded.
"""

import ast
import posixpath
from itertools import product

from code_health.bindings import ModuleUses
from code_health.symbols import enclosing_scope, local_nodes, parameter_names

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


ROOT = "⟨root⟩"
PARAMETER = object()
PATH_MAKERS = frozenset(
    {
        "builtins.str",
        "os.fspath",
        "os.path.abspath",
        "os.path.realpath",
        "os.path.normpath",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PosixPath",
        "pathlib.PurePosixPath",
    }
)
JOINERS = frozenset({"os.path.join", "posixpath.join"})
DIRNAMES = frozenset({"os.path.dirname", "posixpath.dirname"})
SPELLINGS = {"str": "builtins.str", "Path": "pathlib.Path"}
PARAMETRIZE = frozenset({"pytest.mark.parametrize", "_pytest.mark.parametrize"})
PARAMETER_SETS = frozenset({"pytest.param", "_pytest.mark.param"})
STRING_METHODS = ("upper", "lower", "strip", "title", "capitalize", "casefold")
PATH_METHODS = ("resolve", "absolute")


def integer(expression: ast.expr) -> int | None:
    """The integer a literal or a negated literal is."""
    negated = isinstance(expression, ast.UnaryOp) and isinstance(
        expression.op, ast.USub
    )
    literal = expression.operand if negated else expression
    if isinstance(literal, ast.Constant) and type(literal.value) is int:
        return -literal.value if negated else literal.value
    return None


def folded(expression: ast.expr) -> str | None:
    """The string a constant string expression evaluates to: literals, slices
    with constant bounds, concatenation and argument-free case methods."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
        left, right = folded(expression.left), folded(expression.right)
        return None if left is None or right is None else left + right
    if isinstance(expression, ast.Subscript) and isinstance(
        expression.slice, ast.Slice
    ):
        text = folded(expression.value)
        bounds = [expression.slice.lower, expression.slice.upper, expression.slice.step]
        numbers = [None if bound is None else integer(bound) for bound in bounds]
        if text is None or any(
            bound is not None and number is None
            for bound, number in zip(bounds, numbers, strict=True)
        ):
            return None
        return text[slice(*numbers)]
    if (
        isinstance(expression, ast.Call)
        and not expression.args
        and not expression.keywords
        and isinstance(expression.func, ast.Attribute)
        and expression.func.attr in STRING_METHODS
    ):
        text = folded(expression.func.value)
        return None if text is None else getattr(text, expression.func.attr)()
    return None


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


class ModuleStrings:
    """The finite string values of one module's expressions, a name read from
    the one assignment the module's loaded statements give it and a callee
    read as it is spelled."""

    def __init__(self, path: str, loaded: list[ast.stmt]):
        self.path = path
        self.loaded = loaded
        self.folding: set[int] = set()

    def dotted(self, function: ast.expr) -> str:
        spelled = ast.unparse(function)
        return SPELLINGS.get(spelled, spelled)

    def binding(self, name: str, use: ast.expr) -> tuple:
        """The one value the module assigns the name, and the scope that does."""
        assigned = assigned_to(name, self.loaded)
        return (assigned[0] if len(assigned) == 1 else None), None

    def repository_path(self, expression: ast.expr) -> str | None:
        """The one repository path an expression folds to, relative to the
        root; None when it folds to several, to none or outside the root."""
        folded = self.strings(expression)
        if folded is None or len(folded) != 1:
            return None
        text = folded.pop()
        if text == ROOT:
            return ""
        return text.removeprefix(f"{ROOT}/") if text.startswith(f"{ROOT}/") else None

    def strings(self, expression: ast.expr) -> set[str] | None:
        if (text := folded(expression)) is not None:
            return {text}
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
            return self.ancestor(expression.value, 0)
        if isinstance(expression, ast.Subscript):
            holder, index = expression.value, integer(expression.slice)
            if not (isinstance(holder, ast.Attribute) and holder.attr == "parents"):
                return None
            return None if index is None else self.ancestor(holder.value, index)
        if isinstance(expression, ast.Call):
            return self.called(expression)
        return None

    def ancestor(self, expression: ast.expr, index: int) -> set[str] | None:
        """The directories .parents[index] of each value names."""
        below = self.strings(expression)
        if below is None or index < 0:
            return None
        found = set()
        for folder in below:
            for _ in range(index + 1):
                folder = posixpath.dirname(folder)
            found.add(folder)
        return found

    def union(self, elements: list[ast.expr]) -> set[str] | None:
        found: set[str] = set()
        for element in elements:
            texts = self.strings(element)
            if texts is None:
                return None
            found |= texts
        return found

    def formatted(self, node: ast.JoinedStr) -> set[str] | None:
        parts = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.format_spec is not None or part.conversion != -1:
                    return None
                part = part.value
            texts = self.strings(part)
            if texts is None:
                return None
            parts.append(texts)
        return {"".join(pieces) for pieces in product(*parts)}

    def named(self, name: ast.Name) -> set[str] | None:
        if name.id == "__file__":
            return {f"{ROOT}/{self.path}"}
        assigned, binding = self.binding(name.id, name)
        if assigned is None:
            return None
        if assigned is PARAMETER:
            return self.parametrized(binding, name.id)
        if id(assigned) in self.folding:
            return None
        self.folding.add(id(assigned))
        try:
            return self.strings(assigned)
        finally:
            self.folding.discard(id(assigned))

    def parametrized(self, function: ast.AST, name: str) -> set[str] | None:
        """The literal values a parametrize decorator on the function supplies
        for one of its parameters."""
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call) and len(decorator.args) >= 2):
                continue
            if self.dotted(decorator.func) not in PARAMETRIZE:
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
                and self.dotted(element.func) in PARAMETER_SETS
            ):
                found.append(list(element.args))
            else:
                found.append([element])
        return found

    def called(self, call: ast.Call) -> set[str] | None:
        if call.keywords:
            return None
        dotted = self.dotted(call.func)
        method = call.func.attr if isinstance(call.func, ast.Attribute) else None
        if method in PATH_METHODS and not call.args:
            return self.strings(call.func.value)
        if dotted in DIRNAMES and len(call.args) == 1:
            below = self.strings(call.args[0])
            return None if below is None else {posixpath.dirname(p) for p in below}
        if dotted in PATH_MAKERS and len(call.args) == 1:
            return self.strings(call.args[0])
        parts: list[ast.expr] = []
        if dotted in PATH_MAKERS or dotted in JOINERS:
            parts = call.args
        elif method == "joinpath":
            parts = [call.func.value, *call.args]
        if not parts:
            return None
        texts = [self.strings(part) for part in parts]
        return None if None in texts else joined(texts)

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
            texts = self.strings(part)
            if texts is None or len(texts) != 1:
                break
            found += texts.pop()
        return found


class StringValues(ModuleStrings):
    """The finite string values of one module's expressions, a name read in
    the scope that binds it and a callee read through its imports."""

    def __init__(self, module: ModuleUses, loaded: list[ast.stmt]):
        super().__init__(module.path, loaded)
        self.module = module

    def dotted(self, function: ast.expr) -> str:
        return self.module.held(function)[1]

    def binding(self, name: str, use: ast.expr) -> tuple:
        """The one value the scope binding the name, as the use reads it,
        assigns it, with that scope; PARAMETER for a parameter of a function,
        None for several bindings or a class body."""
        scope = enclosing_scope(use, self.module.parent_of)
        binding = self.module.binding_scope(name, scope)
        if isinstance(binding, ast.Module):
            return super().binding(name, use)
        if not isinstance(binding, FUNCTIONS[:2]):
            return None, binding
        if name in parameter_names(binding):
            return PARAMETER, binding
        assigned = assigned_to(name, local_nodes(binding))
        return (assigned[0] if len(assigned) == 1 else None), binding
