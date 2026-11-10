"""Name bindings: whose code a name, an expression or a call holds in one
module, decided from every binding that can reach it.

A name holds the union of what each of its bindings in the scope that binds it
gives it, and resolves only when they all agree: a function or class the scope
defines, an import the repository resolves and an enclosing method's receiver
hold the program's code; an absolute import of a name outside the repository,
a builtin and a literal hold library code; an assignment or with statement
holds what its value holds, where a value built by calling the program is the
program's, one built by library code the library's, and unittest.mock builds
mock objects; a parameter holds the pytest fixture of its name. An import of
the repository's own package that no root resolves, a relative import the
repository does not resolve, a binding the analysis does not evaluate (a loop
target, an augmented assignment, a walrus, an exception or match capture, a
del, a global or nonlocal declaration, an unpacked target) and a name bound
nowhere do not resolve.
"""

import ast
import builtins
import re

from code_health.scopes import parameter_names, parents, receiving_call

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
SCOPES = (*FUNCTIONS, ast.ClassDef)
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
PROGRAM, LIBRARY, MOCK, ASSERTION = "program", "library", "mock", "assertion"
UNRESOLVED = (None, "")
VALUE_LITERALS = (
    ast.Constant,
    ast.JoinedStr,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
MOCK_MODULE = "unittest.mock"
MOCK_ASSERTIONS = frozenset(
    "assert_called assert_called_once assert_called_with assert_called_once_with "
    "assert_any_call assert_has_calls assert_not_called assert_awaited "
    "assert_awaited_once assert_awaited_with assert_awaited_once_with "
    "assert_any_await assert_has_awaits assert_not_awaited".split()
)
CAPTURES = (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)


def enclosing_scope(node: ast.AST, parent_of: dict) -> ast.AST:
    """The scope the node is evaluated in: a nested scope's decorators,
    bases, defaults and annotations belong to the scope around it."""
    child, scope = node, parent_of[id(node)]
    while not isinstance(scope, ast.Module):
        body = scope.body if isinstance(scope, SCOPES) else None
        if child is body or (isinstance(body, list) and child in body):
            return scope
        child, scope = scope, parent_of[id(scope)]
    return scope


def own_nodes(scope: ast.AST):
    """The scope's own nodes: nested scopes are yielded, never entered."""
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    pending = list(body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def bound_names(node: ast.AST) -> list[str]:
    """The names a node of a scope binds in it: a stored name, a definition, an
    import or a capture."""
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    return [node.name] if isinstance(node, (*DEFINITIONS, *CAPTURES)) else []


def rebinds(scope: ast.AST, name: str) -> bool:
    """The scope binds the name as a parameter or in one of its own nodes."""
    if isinstance(scope, FUNCTIONS) and name in parameter_names(scope):
        return True
    return any(name in bound_names(node) for node in own_nodes(scope))


def header(scope: ast.AST) -> list[ast.AST]:
    """The parts of a nested scope its enclosing scope evaluates."""
    if isinstance(scope, ast.ClassDef):
        return [*scope.decorator_list, *scope.bases, *scope.keywords]
    if isinstance(scope, ast.Lambda):
        return [scope.args]
    returns = [scope.returns] if scope.returns else []
    return [*scope.decorator_list, scope.args, *returns]


def annotation_slots(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, (ast.AnnAssign, ast.arg)):
        return [node.annotation] if node.annotation else []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
        return [node.returns]
    return []


def references(scope: ast.AST, name: str) -> list[ast.expr]:
    """The loads of a name bound in the scope, and the annotation strings that
    name it. A class scope's names are invisible inside its nested scopes; a
    function or module scope's names are visible in nested scopes that do not
    rebind them."""
    found: list[ast.expr] = []
    word = re.compile(rf"\b{re.escape(name)}\b")
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, SCOPES):
            pending.extend(header(node))
            if not isinstance(scope, ast.ClassDef) and not rebinds(node, name):
                body = node.body if isinstance(node.body, list) else [node.body]
                pending.extend(body)
            continue
        if (
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Load)
        ):
            found.append(node)
        found.extend(
            text
            for slot in annotation_slots(node)
            for text in ast.walk(slot)
            if isinstance(text, ast.Constant)
            and isinstance(text.value, str)
            and word.search(text.value)
        )
        pending.extend(ast.iter_child_nodes(node))
    return found


def call_arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def import_names(node: ast.Import | ast.ImportFrom):
    """The name each alias of an import statement binds, and the dotted path
    it stands for."""
    for alias in node.names:
        if isinstance(node, ast.ImportFrom):
            yield alias.asname or alias.name, f"{node.module}.{alias.name}"
        else:
            bound = alias.asname or alias.name.split(".")[0]
            yield bound, alias.name if alias.asname else bound


def simple_targets(node: ast.AST) -> list[tuple[ast.Name, ast.expr]]:
    """The plain names an assignment or with item binds, each with the value
    it receives."""
    if isinstance(node, ast.Assign):
        return [(t, node.value) for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        target = node.target
        return [(target, node.value)] if isinstance(target, ast.Name) else []
    if isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
        return [(node.optional_vars, node.context_expr)]
    return []


def fixture_provides(module: "ModuleUses", body_nodes) -> tuple:
    """What a fixture returns or yields, from its own nodes, when every return
    and yield agrees."""
    produced = {
        module.held(node.value)
        for node in body_nodes
        if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None
    }
    return produced.pop() if len(produced) == 1 else UNRESOLVED


class ModuleUses:
    """Whose code each name, expression and call of one module holds, given
    the import statements the repository resolves (by statement and bound
    name), the repository's own top-level packages and the fixture resolver."""

    def __init__(
        self,
        path: str,
        tree: ast.Module,
        first_party: set[tuple[int, str]],
        packages: set[str],
        fixture,
    ):
        self.path = path
        self.parent_of = parents(tree)
        self.star_imported = any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "*" for alias in node.names)
            for node in own_nodes(tree)
        )
        self.first_party = first_party
        self.packages = packages
        self.fixture = fixture
        self.visiting: set[tuple[int, str]] = set()

    def attribute(self, use: ast.expr, chain: list[str]) -> ast.expr | None:
        """The attribute chain read from the use, when the use is followed by
        exactly those attribute names."""
        for name in chain:
            parent = self.parent_of.get(id(use))
            if not (isinstance(parent, ast.Attribute) and parent.attr == name):
                return None
            use = parent
        return use

    def origin(self, name: str, scope: ast.AST) -> tuple:
        """What the name holds in the scope that binds it: what all its
        bindings there agree on, or unresolved."""
        if (id(scope), name) in self.visiting:
            return UNRESOLVED
        self.visiting.add((id(scope), name))
        try:
            origins = self.bindings(name, scope)
        finally:
            self.visiting.discard((id(scope), name))
        return origins.pop() if len(origins) == 1 else UNRESOLVED

    def bindings(self, name: str, scope: ast.AST) -> set[tuple]:
        """What each binding of the name in the scope gives it; a binding to
        None, the fallback for an absent dependency, holds nothing and does not
        contest the others."""
        found: set[tuple] = set()
        evaluated: set[int] = set()
        absent = False
        for node in own_nodes(scope):
            if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                return {UNRESOLVED}
            found |= self.declared(node, name)
            if isinstance(node, ast.AnnAssign) and node.value is None:
                evaluated.add(id(node.target))
            for target, assigned in simple_targets(node):
                if target.id != name:
                    continue
                evaluated.add(id(target))
                if isinstance(assigned, ast.Constant) and assigned.value is None:
                    absent = True
                else:
                    found.add(self.held(assigned))
        if absent and not found:
            found.add((LIBRARY, ""))
        found |= {
            UNRESOLVED
            for node in own_nodes(scope)
            if isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in evaluated
        }
        if isinstance(scope, FUNCTIONS) and name in parameter_names(scope):
            found.add(self.parameter(scope, name))
        if not found and isinstance(scope, ast.Module) and hasattr(builtins, name):
            found.add(
                UNRESOLVED if self.star_imported else (LIBRARY, f"builtins.{name}")
            )
        return found or {UNRESOLVED}

    def declared(self, node: ast.AST, name: str) -> set[tuple]:
        """What a definition, import or capture statement binds the name to."""
        if isinstance(node, DEFINITIONS) and node.name == name:
            return {(PROGRAM, "")}
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return {
                self.imported(node, bound, dotted)
                for bound, dotted in import_names(node)
                if bound == name
            }
        if isinstance(node, CAPTURES) and node.name == name:
            return {UNRESOLVED}
        return set()

    def parameter(self, function: ast.AST, name: str) -> tuple:
        """A method's receiver holds the program's code; any other parameter
        holds the pytest fixture of its name."""
        receiver = isinstance(self.parent_of[id(function)], ast.ClassDef)
        if receiver and parameter_names(function)[:1] == [name]:
            return PROGRAM, ""
        return self.fixture(function, name)

    def imported(self, node: ast.Import | ast.ImportFrom, bound: str, dotted: str):
        """What one name an import statement binds holds."""
        if (id(node), bound) in self.first_party:
            return PROGRAM, ""
        relative = isinstance(node, ast.ImportFrom) and node.level
        if relative or dotted.split(".")[0] in self.packages:
            return UNRESOLVED
        return LIBRARY, dotted

    def binding_scope(self, name: str, scope: ast.AST) -> ast.AST:
        """The scope whose bindings a load of the name evaluated in the scope
        reads: the scope itself, then each enclosing function, then the
        module; an enclosing class body is not visible."""
        while not isinstance(scope, ast.Module):
            if isinstance(scope, FUNCTIONS) and name in parameter_names(scope):
                return scope
            if any(name in bound_names(node) for node in own_nodes(scope)):
                return scope
            scope = enclosing_scope(scope, self.parent_of)
            while isinstance(scope, ast.ClassDef):
                scope = enclosing_scope(scope, self.parent_of)
        return scope

    def held(self, expression: ast.expr) -> tuple:
        """What an expression holds, read in the scope that encloses it."""
        scope = enclosing_scope(expression, self.parent_of)
        root, chain, called = expression, [], False
        while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript, ast.Await)):
            if isinstance(root, ast.Attribute):
                chain.insert(0, root.attr)
            called |= isinstance(root, ast.Call)
            root = root.func if isinstance(root, ast.Call) else root.value
        if isinstance(root, VALUE_LITERALS):
            return LIBRARY, ""
        if not isinstance(root, ast.Name):
            return UNRESOLVED
        kind, dotted = self.origin(root.id, self.binding_scope(root.id, scope))
        dotted = ".".join([dotted, *chain]) if dotted else ""
        if kind == LIBRARY and called and dotted.startswith(f"{MOCK_MODULE}."):
            return MOCK, ""
        return kind, dotted

    def callee(self, call: ast.Call) -> str | None:
        """Whose code the call runs: the program's, library code's, an
        assertion method of a mock object, or None when it does not resolve."""
        function = call.func
        receiver = function.value if isinstance(function, ast.Attribute) else function
        kind, _ = self.held(receiver)
        if kind == MOCK:
            attribute = getattr(function, "attr", "")
            return ASSERTION if attribute in MOCK_ASSERTIONS else LIBRARY
        return kind

    def handed_to(self, expression: ast.expr) -> str | None:
        """Whose code the call the expression is handed to runs; the program's
        when no call receives it."""
        call = receiving_call(expression, self.parent_of)
        return PROGRAM if call is None else self.callee(call)

    def instance_handed_to(self, call: ast.Call) -> str | None:
        """Whose code receives the instance: the call it goes straight to, or
        the calls every load of the one name it is assigned to goes to."""
        statement = self.parent_of[id(call)]
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            return self.handed_to(call)
        loads = references(
            enclosing_scope(statement, self.parent_of), statement.targets[0].id
        )
        receivers = {self.handed_to(use) for use in loads if isinstance(use, ast.Name)}
        if not receivers or PROGRAM in receivers:
            return PROGRAM
        return None if None in receivers else LIBRARY
