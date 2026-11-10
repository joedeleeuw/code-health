"""Specimens: classes the program hands to library code instead of holding them
as shapes of its own, told apart by how the program uses each class.

Every call is resolved through the bindings of the names it is made on
(ModuleUses): the program's own code, library code, an assertion method of a
unittest.mock object, or unresolved. A class is a specimen when every use hands
the class, or an instance built and consumed on the spot, to library code, such
as attrs.fields(C), cattrs.structure(d, C) or repr(C()). No annotation may name
the class, and none of its own methods may read its fields. A use handed to a
call that does not resolve leaves the class undecided. A class nothing refers
to is a specimen only when something resolved runs its module, which proves
nothing hands it anywhere: a repository module imports it, by statement or by a
module name that folds (code_health.folding), it guards code to run as a
script, the test runner collects it, or a distribution ships it
(code_health.distribution). Otherwise the class is undecided, named with what
may run the module: a build configuration that does not resolve, an import of
its package no root resolves, or a computed module name imported at run time;
the absence of such evidence proves nothing.

A module is a type-checker input when a type checker receives it and nothing
resolved runs it; uses inside the module itself are not evidence that it runs.
A type checker receives the files and directories its
configuration names (code_health.projects), and those a call hands, as strings
that fold, to a type checker's api, main, run or cli object or to a callable
defined in a repository module that imports a type checker; the callee is
resolved through
the calling module's imports, and one that does not resolve marks nothing. A
string is read relative to the calling module or to the repository root, and
"." names everything below. A checker call whose arguments do not fold is
named as what may make an unreached module a type-checker input.

A class is a runner input when it is declared inside a function the test
runner collects (code_health.collection), every use of the class is inside
that function, and neither the class nor a value built from it leaves the
function: no return or yield carries it, no store puts it anywhere but a name
the function itself binds, and no call outside an assert statement hands it to
library code, such as a framework, rather than to the repository's code under
test or to a mock's assertion method. A call that does not resolve, or
collection the runner decides in code, leaves the class undecided. A parameter
of a test or fixture holds the value of the fixture of its name that the
module, or a conftest.py beside or above it, defines or imports, or pytest's
own fixture of that name.
"""

import ast
import posixpath
from dataclasses import dataclass
from functools import partial

from code_health.bindings import (
    LIBRARY,
    PROGRAM,
    SCOPES,
    ModuleUses,
    annotation_slots,
    call_arguments,
    enclosing_scope,
    fixture_provides,
    own_nodes,
    references,
)
from code_health.collection import TestCollection
from code_health.distribution import Distributions
from code_health.folding import ROOT, StringValues
from code_health.imports import Modules, module_name, runs_as_script
from code_health.scopes import parameter_names
from code_health.projects import Projects

TYPE_CHECKERS = frozenset({"mypy", "pyright", "basedpyright", "pytype", "pyre_check"})
CHECKER_ENTRY_POINTS = frozenset({"api", "main", "run", "cli"})
DYNAMIC_IMPORTERS = frozenset({"importlib.import_module", "builtins.__import__"})
PYTEST_FIXTURES = frozenset(
    "cache capsys capsysbinary capfd capfdbinary caplog doctest_namespace "
    "monkeypatch pytestconfig record_property record_xml_attribute "
    "record_testsuite_property recwarn request tmp_path tmp_path_factory tmpdir "
    "tmpdir_factory pytester testdir subtests".split()
)
FIXTURE_DECORATORS = ("pytest.fixture", "_pytest.fixtures.fixture")


def annotated(node: ast.expr, parent_of: dict) -> bool:
    if isinstance(node, ast.Constant):
        return True
    child, parent = node, parent_of.get(id(node))
    while isinstance(parent, ast.expr):
        child, parent = parent, parent_of.get(id(parent))
    return any(child is slot for slot in annotation_slots(parent))


def scoped_imports(scope: ast.AST):
    """Each import statement under the scope, with the scope it binds names in."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, SCOPES):
            yield from scoped_imports(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            yield scope, node
        else:
            pending.extend(ast.iter_child_nodes(node))


def within(node: ast.AST, scope: ast.AST, parent_of: dict) -> bool:
    while node is not None and node is not scope:
        node = parent_of.get(id(node))
    return node is scope


def carried_names(function: ast.AST, uses: set[int]) -> set[str] | None:
    """The names the function binds from a use of the class, or from such a
    name; None when such a value is stored anywhere but a name the function
    binds, a parameter belonging to the caller."""
    local = {
        target.id
        for target in ast.walk(function)
        if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
    } - set(parameter_names(function))
    carried: set[str] = set()
    stores = [
        statement
        for statement in ast.walk(function)
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and statement.value is not None
    ]
    for _ in range(2):
        for statement in stores:
            if not any(
                id(part) in uses or (isinstance(part, ast.Name) and part.id in carried)
                for part in ast.walk(statement.value)
            ):
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            for target in targets:
                root = target
                while isinstance(root, (ast.Attribute, ast.Subscript)):
                    root = root.value
                if not (isinstance(root, ast.Name) and root.id in local):
                    return None
                if isinstance(target, ast.Name):
                    carried.add(target.id)
    return carried


def escapes(function: ast.AST, uses: set[int], module: "ModuleUses") -> bool | str:
    """Whether a use of the class, or a value built from one, leaves the
    function: a return or yield carries it, it is stored beyond the function's
    own names or through a global or nonlocal, or a call outside an assert
    statement hands it to library code rather than to the repository's code
    under test or to an assertion method of a unittest.mock object. A call whose
    callee does not resolve leaves it undecided, named in the result."""
    carried = carried_names(function, uses)
    if carried is None or any(
        isinstance(statement, (ast.Global, ast.Nonlocal))
        for statement in ast.walk(function)
    ):
        return True

    def reaches(expression: ast.AST) -> bool:
        return any(
            id(part) in uses or (isinstance(part, ast.Name) and part.id in carried)
            for part in ast.walk(expression)
        )

    asserted = {
        id(part)
        for statement in ast.walk(function)
        if isinstance(statement, ast.Assert)
        for part in ast.walk(statement)
    }
    if any(
        isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom))
        and node.value is not None
        and reaches(node.value)
        for node in ast.walk(function)
    ):
        return True
    unresolved = []
    for call in ast.walk(function):
        if not (
            isinstance(call, ast.Call)
            and id(call) not in asserted
            and any(reaches(argument) for argument in call_arguments(call))
        ):
            continue
        callee = module.callee(call)
        if callee == LIBRARY:
            return True
        if callee is None:
            unresolved.append(
                f"{ast.unparse(call.func)} at {module.path}:{call.lineno}"
            )
    if not unresolved:
        return False
    return f"the unresolved callee {', '.join(sorted(set(unresolved)))}"


def checker_entry_point(dotted: str) -> bool:
    """A type checker's own object that runs a check, not one of the types its
    plugin API exposes: a path under a checker package through an api, main,
    run or cli module."""
    parts = dotted.split(".")
    return parts[0] in TYPE_CHECKERS and bool(set(parts[1:]) & CHECKER_ENTRY_POINTS)


def enclosing_functions(parent_of: dict[int, ast.AST], scope: ast.AST) -> list:
    """The functions a class is declared inside, nearest first, up to the
    module or an enclosing class; the outermost one the test runner collects,
    or a pytest fixture, owns the class as its input, whatever helper nests
    it."""
    found = []
    while not isinstance(scope, (ast.Module, ast.ClassDef)):
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.append(scope)
        scope = parent_of[id(scope)]
    return found


@dataclass
class LocalClass:
    """The evidence about a class declared inside a function: whether the test
    runner collects the function, whether every use of the class stays inside
    it, and whether a value built from the class leaves it; the first and last
    name what leaves them undecided."""

    collected: bool | str
    confined: bool
    escaped: bool | str

    def runner_input(self) -> bool | str:
        """Whether the class is a runner input: declared in a function the test
        runner collects, every use inside it, and nothing built from the class
        leaving it; otherwise what leaves that undecided."""
        if self.collected is False or not self.confined or self.escaped is True:
            return False
        undecided = [
            why for why in (self.collected, self.escaped) if isinstance(why, str)
        ]
        return " and ".join(undecided) or True

    def collected_escape(self) -> bool | str:
        """Whether a value built from the class provably leaves the function
        the test runner collects; what leaves the collection or the escape
        undecided otherwise."""
        if self.collected is False or self.escaped is False:
            return False
        if self.collected is True and self.escaped is True:
            return True
        return " and ".join(
            why for why in (self.collected, self.escaped) if isinstance(why, str)
        )


class Specimens:
    """Which classes of the repository are specimens, from the uses of each class
    across the repository's modules, followed through imports."""

    def __init__(self, modules: Modules, projects: Projects, shipped: Distributions):
        self.modules = modules
        self.shipped = shipped
        self.trees = modules.trees
        self.module_uses: dict[str, ModuleUses] = {}
        self.class_imports: dict[tuple[str, str], list] = {}
        self.module_imports: dict[str, list] = {}
        self.imported: set[str] = set()
        self.first_party: dict[str, set[tuple[int, str]]] = {}
        self.checker_importers: set[str] = set()
        for path, tree in self.trees.items():
            for scope, node in scoped_imports(tree):
                self.index_import(modules, path, scope, node)
                written = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if any(name.split(".")[0] in TYPE_CHECKERS for name in written):
                    self.checker_importers.add(path)
        self.unresolved_tops = {
            written.split(".")[0]
            for sites in modules.unresolved.values()
            for _, written in sites
        }
        self.memo: dict[int, list[tuple[str, ast.expr]]] = {}
        self.resolving: set[tuple[str, int, str]] = set()
        self.values: dict[str, StringValues] = {}
        self.computed_imports: list[tuple[str, str]] = []
        self.unfolded_checks: list[str] = []
        self.runner = TestCollection(modules, projects)
        for path in self.trees:
            self.dynamic_imports(path)
        called = {
            checked for path in self.trees for checked in self.checked_paths(path)
        }
        self.checked = projects.checked | called
        self.checked_roots = projects.checked_roots | called
        self.fixtures: dict[str, bool] = {}

    def strings(self, path: str) -> StringValues:
        if path not in self.values:
            self.values[path] = StringValues(self.module(path), self.trees[path])
        return self.values[path]

    def dynamic_imports(self, path: str) -> None:
        """Record the repository modules the module imports by a name that
        folds, and the literal prefix of each computed name that does not."""
        module = self.module(path)
        for call in ast.walk(self.trees[path]):
            if not (isinstance(call, ast.Call) and call.args):
                continue
            if module.held(call.func)[1] not in DYNAMIC_IMPORTERS:
                continue
            names = self.strings(path).strings(call.args[0])
            if names is None:
                prefix = self.strings(path).prefix(call.args[0])
                self.computed_imports.append(
                    (self.absolute_name(path, prefix), f"{path}:{call.lineno}")
                )
                continue
            for dotted in names:
                found = self.modules.absolute(
                    self.modules.bases(path), self.absolute_name(path, dotted), []
                )[0]
                if found is not None:
                    self.imported.add(found)

    def absolute_name(self, path: str, dotted: str) -> str:
        """A module name imported at run time as absolute: a relative one is
        read against the importing module's package."""
        if not dotted.startswith("."):
            return dotted
        package = module_name(path, set(self.trees)).rsplit(".", 1)[0]
        return f"{package}.{dotted.lstrip('.')}".rstrip(".")

    def reach(self, path: str) -> bool | str:
        """Whether something resolved runs the module: a repository import, a
        main guard, the test runner's collection or a distribution shipping it;
        otherwise what may run it, or False when nothing named does."""
        collection = self.runner.collection(path)
        shipping = self.shipped.shipping(path)
        if (
            path in self.imported
            or runs_as_script(self.trees[path])
            or (collection is not None and collection.collects_file(path))
            or shipping is True
        ):
            return True
        dotted = module_name(path, set(self.trees))
        reasons = []
        if dotted.split(".")[0] in self.unresolved_tops:
            reasons.append(
                f"an import of {dotted.split('.')[0]} that no searched root resolves"
            )
        if shipping is None:
            reasons.append("the unresolved build configuration that may ship it")
        reasons += [
            f"the computed module name imported at {site}"
            for prefix, site in self.computed_imports
            if dotted.startswith(prefix)
        ]
        return " and ".join(reasons) or False

    def typing_fixture(self, path: str) -> bool:
        """Whether a type checker alone reads the module: a file or directory a
        checker is told to read holds it, and nothing resolved runs any module
        that entry holds. A module nothing runs beside running code under one
        entry is not one: nothing tells it from dead code."""
        for root in self.checked_roots:
            if root and path != root and not path.startswith(f"{root}/"):
                continue
            if root not in self.fixtures:
                self.fixtures[root] = not any(
                    self.reach(held) is not False
                    for held in self.trees
                    if not root or held == root or held.startswith(f"{root}/")
                )
            if self.fixtures[root]:
                return True
        return False

    def callee_origin(self, path: str, call: ast.Call) -> str | None:
        """Where the called object is defined: a repository file, or the dotted
        name of an external module's object; None when the binding of the
        callee's name at the call does not resolve to either."""
        chain = []
        head = call.func
        while isinstance(head, ast.Attribute):
            chain.insert(0, head.attr)
            head = head.value
        if not isinstance(head, ast.Name):
            return None
        module = self.module(path)
        scope = enclosing_scope(call, module.parent_of)
        kind, dotted = module.held(call.func)
        if kind == LIBRARY:
            return dotted or None
        if kind != PROGRAM or not isinstance(
            module.binding_scope(head.id, scope), ast.Module
        ):
            return None
        origin = self.modules.exports(path).get(head.id)
        if origin is None or origin == path:
            return origin
        for attribute in chain:
            exported = self.modules.exports(origin)
            if attribute not in exported or exported[attribute] == origin:
                return origin
            origin = exported[attribute]
        return origin

    def checked_paths(self, path: str) -> set[str]:
        """The repository files and directories the module's calls hand to a
        type checker as strings that fold; "" stands for the whole
        repository. A call of the checker's own code, or one that names a
        repository path, with an argument that does not fold is recorded by
        its site."""
        found = set()
        for call in ast.walk(self.trees[path]):
            if not (isinstance(call, ast.Call) and (call.args or call.keywords)):
                continue
            origin = self.callee_origin(path, call)
            if origin is None or not (
                origin in self.checker_importers or checker_entry_point(origin)
            ):
                continue
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            folded = [self.strings(path).strings(argument) for argument in arguments]
            named = set()
            for text in {value for values in folded if values for value in values}:
                named.update(self.located(path, text))
            found.update(named)
            if None in folded and (named or origin not in self.trees):
                self.unfolded_checks.append(
                    f"{ast.unparse(call.func)} at {path}:{call.lineno}"
                )
        return found

    def located(self, path: str, text: str) -> set[str]:
        """The repository files and directories a string a module hands to a
        type checker names: under the repository root when __file__ rooted it,
        otherwise relative to the module or to the root."""
        if text == ROOT or text.startswith(f"{ROOT}/"):
            candidates = [text[len(ROOT) + 1 :]]
        else:
            candidates = [posixpath.join(posixpath.dirname(path), text), text]
        found = set()
        for candidate in candidates:
            named = posixpath.normpath(candidate)
            named = "" if named == "." else named
            if named == "" or named in self.trees:
                found.add(named)
            elif any(known.startswith(f"{named}/") for known in self.trees):
                found.add(named)
        return found

    def type_checker_input(self, path: str) -> bool | str:
        """Whether a type checker receives the module and nothing resolved runs
        it; what may run it, or the checker call that may hand it over, when
        that is undecided."""
        received = (
            "" in self.checked
            or path in self.checked
            or any(
                path.startswith(f"{checked}/") for checked in self.checked if checked
            )
        )
        reach = self.reach(path)
        if reach is True or not (received or self.unfolded_checks):
            return False
        if isinstance(reach, str):
            return reach
        if received:
            return True
        sites = ", ".join(sorted(set(self.unfolded_checks)))
        return f"the type-checker call at {sites}, whose paths do not fold"

    def local_evidence(self, path: str, node: ast.ClassDef) -> "LocalClass | None":
        """What decides the role of a class declared inside a function: whether
        the test runner collects the function, whether every use of the class
        stays inside it, and whether a value built from the class leaves it;
        None for a class declared outside any function."""
        parent_of = self.module(path).parent_of
        functions = enclosing_functions(parent_of, parent_of[id(node)])
        if not functions:
            return None
        function, collected = functions[0], self.runner.collected(path, functions[0])
        fixture = False
        for outer in functions:
            if self.fixture_function(self.module(path), outer):
                function, collected, fixture = outer, True, True
            elif self.runner.collected(path, outer) is True:
                function, collected, fixture = outer, True, False
        uses = self.uses(path, node)
        parent_of = self.module(path).parent_of
        return LocalClass(
            collected,
            all(
                user == path and within(use, function, parent_of) for user, use in uses
            ),
            False
            if fixture
            else escapes(function, {id(use) for _, use in uses}, self.module(path)),
        )

    def index_import(self, modules: Modules, path: str, scope, node) -> None:
        imported = modules.imported(path, node)
        for (module, name), alias in zip(imported, node.names, strict=True):
            if module is None:
                continue
            bound = alias.asname or alias.name.split(".")[0]
            if name:
                bound = alias.asname or name
            self.first_party.setdefault(path, set()).add((id(node), bound))
            if module == path:
                continue
            self.imported.add(module)
            if name == "*":
                continue
            if name:
                owner = modules.defining(module, name)
                self.imported.add(owner)
                binding = (path, scope, alias.asname or name)
                self.class_imports.setdefault((owner, name), []).append(binding)
            elif isinstance(node, ast.ImportFrom) or alias.asname:
                binding = (path, scope, alias.asname or alias.name, [])
                self.module_imports.setdefault(module, []).append(binding)
            else:
                root, *chain = alias.name.split(".")
                binding = (path, scope, root, chain)
                self.module_imports.setdefault(module, []).append(binding)

    def module(self, path: str) -> ModuleUses:
        if path not in self.module_uses:
            self.module_uses[path] = ModuleUses(
                path,
                self.trees[path],
                self.first_party.get(path, set()),
                self.modules.packages,
                partial(self.fixture, path),
            )
        return self.module_uses[path]

    @staticmethod
    def fixture_function(module: ModuleUses, function: ast.AST) -> bool:
        """The function is decorated as a pytest fixture."""
        return any(
            module.held(
                decorator.func if isinstance(decorator, ast.Call) else decorator
            )[1]
            in FIXTURE_DECORATORS
            for decorator in function.decorator_list
        )

    def fixture(self, path: str, function: ast.AST, name: str) -> tuple:
        """What a parameter holds: when the function is a test or a fixture,
        the value of the fixture of that name the module, or a conftest.py
        beside or above it, defines or imports, or pytest's own fixture of that
        name, a library object; otherwise it does not resolve."""
        if (path, id(function), name) in self.resolving or not isinstance(
            function, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            return None, ""
        test = self.runner.collected(path, function) is not False
        if not (test or self.fixture_function(self.module(path), function)):
            return None, ""
        self.resolving.add((path, id(function), name))
        try:
            candidates = [path, *self.runner.conftests(path)]
            for candidate in candidates:
                defining = self.modules.exports(candidate).get(name)
                definition = defining and next(
                    (
                        node
                        for node in self.trees[defining].body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == name
                        and self.fixture_function(self.module(defining), node)
                    ),
                    None,
                )
                if definition:
                    return fixture_provides(
                        self.module(defining), own_nodes(definition)
                    )
        finally:
            self.resolving.discard((path, id(function), name))
        if name in PYTEST_FIXTURES:
            return LIBRARY, f"pytest.{name}"
        return None, ""

    def uses(self, path: str, node: ast.ClassDef) -> list[tuple[str, ast.expr]]:
        """Each expression in the repository that evaluates to the class."""
        if id(node) in self.memo:
            return self.memo[id(node)]
        scope = enclosing_scope(node, self.module(path).parent_of)
        found = [(path, use) for use in references(scope, node.name)]
        if isinstance(scope, ast.Module):
            for user, where, binding in self.class_imports.get((path, node.name), []):
                found += [(user, use) for use in references(where, binding)]
            for user, where, binding, chain in self.module_imports.get(path, []):
                found += [
                    (user, attribute)
                    for use in references(where, binding)
                    if (
                        attribute := self.module(user).attribute(
                            use, [*chain, node.name]
                        )
                    )
                ]
        elif isinstance(scope, ast.ClassDef):
            found += [
                (user, attribute)
                for user, use in self.uses(path, scope)
                if (attribute := self.module(user).attribute(use, [node.name]))
            ]
            found += [
                (path, attribute)
                for method in scope.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (receiver := parameter_names(method)[:1])
                for attribute in ast.walk(method)
                if isinstance(attribute, ast.Attribute)
                and attribute.attr == node.name
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id == receiver[0]
            ]
        self.memo[id(node)] = found
        return found

    def specimen(self, path: str, node: ast.ClassDef, fields: set[str]) -> bool | str:
        """Whether the class is a specimen, or what leaves that undecided: the
        unresolved callees receiving it, or, for a class nothing refers to,
        nothing resolved running its module."""
        uses = self.uses(path, node)
        if not uses:
            reach = self.reach(path)
            if reach is True:
                return False
            return reach or (
                f"the absence of any import, main guard, test collection or "
                f"distribution running {path}"
            )
        unresolved = []
        for user, use in uses:
            module = self.module(user)
            parent = module.parent_of.get(id(use))
            if annotated(use, module.parent_of):
                return False
            if isinstance(parent, ast.Call) and parent.func is use:
                consumer = module.instance_handed_to(parent)
            else:
                consumer = module.handed_to(use)
            if consumer == PROGRAM:
                return False
            if consumer is None:
                unresolved.append(f"receiving it at {user}:{use.lineno}")
        if any(
            isinstance(read, ast.Attribute)
            and isinstance(read.ctx, ast.Load)
            and read.attr in fields
            and isinstance(read.value, ast.Name)
            and read.value.id == receiver[0]
            for method in node.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (receiver := parameter_names(method)[:1])
            for read in ast.walk(method)
        ):
            return False
        if not unresolved:
            return True
        return f"the unresolved callee {', '.join(sorted(set(unresolved)))}"
