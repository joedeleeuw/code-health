"""Name consumers: calls that look a function up by its name, so deleting the
function breaks them. A builtin getattr-family lookup, unittest.mock's
patch.object and patch.multiple, and pytest's MonkeyPatch.setattr and delattr.

A consumer is recognized by the resolved identity of its callee, through any
alias, and decided by a single reaching binding. The usual spellings only
locate calls whose callee does not resolve; such a call is incomplete
analysis, never a consumer. A MonkeyPatch receiver is pytest's object when a
binding shows it: the monkeypatch fixture pytest injects into a test function
or fixture it collects, a parameter annotated pytest.MonkeyPatch, or an object
built by pytest.MonkeyPatch() or MonkeyPatch.context().
"""

import ast
from pathlib import PurePosixPath

from code_health.reaching import Reaching
from code_health.symbols import FUNCTIONS, Evaluation, Symbols, terminal

LOOKUPS = {"getattr", "hasattr", "setattr", "delattr"}
CONSUMERS = {f"builtins.{name}" for name in LOOKUPS} | {
    "unittest.mock.patch.object",
    "mock.patch.object",
}
CONSUMER_SPELLINGS = LOOKUPS | {"object"}
MULTIPLE_PATCHES = {"unittest.mock.patch.multiple", "mock.patch.multiple"}
MONKEYPATCH_METHODS = {"setattr", "delattr"}
MONKEYPATCH = {"pytest.MonkeyPatch", "_pytest.monkeypatch.MonkeyPatch"}
MONKEYPATCH_CONTEXTS = {f"{owner}.context" for owner in MONKEYPATCH}
FIXTURES = {"pytest.fixture", "_pytest.fixtures.fixture"}


def outside_names(resolution: list | None) -> set[str]:
    return {target for kind, target in resolution or [] if kind == "outside"}


def string(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


class NameConsumers:
    """The name consumers of one repository."""

    def __init__(self, symbols: Symbols, reaching: Reaching, parent_of: dict):
        self.symbols = symbols
        self.reaching = reaching
        self.parent_of = parent_of

    def consumer(self, path: str, constant: ast.Constant) -> tuple | None:
        """For a string handed as the attribute name of a call: ("lookup",
        object) when the callee is decided to be a name consumer,
        ("unresolved", None) when a call spelled like one has a callee that is
        not decided, None otherwise."""
        call = self.parent_of[path].get(id(constant))
        if not (isinstance(call, ast.Call) and constant in call.args[1:2]):
            return None
        evaluation = Evaluation(self.symbols, path, call, {})
        spelled = terminal(call.func) in CONSUMER_SPELLINGS
        if self.monkeypatching(evaluation, call):
            return "lookup", call.args[0]
        return self.decided(evaluation, call.func, CONSUMERS, spelled, call.args[0])

    def multiple(self, path: str, call: ast.Call) -> tuple | None:
        """The same decision for the keyword names of patch.multiple."""
        evaluation = Evaluation(self.symbols, path, call, {})
        spelled = terminal(call.func) == "multiple"
        receiver = call.args[0] if call.args else None
        decision = self.decided(
            evaluation, call.func, MULTIPLE_PATCHES, spelled, receiver
        )
        if decision is not None and decision[0] == "lookup" and receiver is None:
            return "unresolved", None
        return decision

    def decided(self, evaluation, callee, owners: set[str], spelled: bool, receiver):
        resolution = evaluation.resolve(callee)
        decided = resolution is not None and self.reaching.certain(evaluation, callee)
        if decided and len(resolution) == 1 and outside_names(resolution) & owners:
            return "lookup", receiver
        if spelled and not decided:
            return "unresolved", None
        return None

    def monkeypatching(self, evaluation: Evaluation, call: ast.Call) -> bool:
        """The call sets or deletes an attribute of an object through an object
        a binding shows is pytest's MonkeyPatch."""
        callee = call.func
        if not (
            isinstance(callee, ast.Attribute)
            and callee.attr in MONKEYPATCH_METHODS
            and isinstance(callee.value, ast.Name)
            and not string(call.args[0])
        ):
            return False
        receiver = callee.value.id
        scope = evaluation.binding_scope(receiver)
        return scope is not None and self.monkeypatch_binding(scope, receiver)

    def monkeypatch_binding(self, scope: ast.AST, receiver: str) -> bool:
        """The receiver's one binding in its scope shows pytest's MonkeyPatch."""
        bindings = self.symbols.binders(scope)[receiver]
        if len(bindings) != 1:
            return False
        binding = bindings[0]
        path = self.symbols.files[id(scope)]
        defining = Evaluation(self.symbols, path, scope, {})
        if isinstance(binding, ast.arg):
            if binding.annotation is not None:
                annotated = defining.resolve(binding.annotation)
                return bool(outside_names(annotated) & MONKEYPATCH)
            return binding.arg == "monkeypatch" and self.pytest_injects(path, scope)
        holder = self.parent_of[path].get(id(binding))
        if isinstance(holder, ast.Assign) and isinstance(holder.value, ast.Call):
            built = defining.resolve(holder.value.func)
            return bool(outside_names(built) & MONKEYPATCH)
        if isinstance(holder, ast.withitem) and isinstance(
            holder.context_expr, ast.Call
        ):
            entered = defining.resolve(holder.context_expr.func)
            return bool(outside_names(entered) & MONKEYPATCH_CONTEXTS)
        return False

    def pytest_injects(self, path: str, function) -> bool:
        """pytest fills the function's parameters with fixtures: it is a fixture
        (decorated with pytest.fixture) or a test function pytest collects by
        its default rules (a test* function at module level or in a Test* class,
        in a test_*.py or *_test.py file)."""
        if not isinstance(function, FUNCTIONS):
            return False
        owner = self.symbols.scopes(path, function)[0]
        defining = Evaluation(self.symbols, path, owner, {})
        for decorator in function.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if outside_names(defining.resolve(target)) & FIXTURES:
                return True
        name = PurePosixPath(path).name
        collected_file = name.startswith("test_") or name.endswith("_test.py")
        collected_owner = isinstance(owner, ast.Module) or (
            isinstance(owner, ast.ClassDef) and owner.name.startswith("Test")
        )
        return collected_file and collected_owner and function.name.startswith("test")
