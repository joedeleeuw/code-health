"""Test collection: whether the test runner collects a function.

pytest collects by its nearest configuration's python_files, python_classes and
python_functions (code_health.projects), unittest a test method of a TestCase
subclass; the external test bases known to derive from unittest.TestCase are
listed in UNITTEST_CASES, and a class deriving from another base that is not
read may be one. In a module pytest collects, a collection hook
(pytest_pycollect_makeitem, pytest_pycollect_makemodule, pytest_ignore_collect)
that a conftest.py beside or above the module binds, or that a plugin it or the
configuration's -p option names binds, decides collection in code, so
collection is undecided; so it is when such a conftest.py star-imports a module
that does not resolve, runs exec() or eval(), or builds its ignore lists at run
time, and when it or the configuration enables a plugin whose code is not read:
one outside the repository, or one of the repository's packages that does not
resolve. A plugin the configuration blocks with -p no: is not loaded, and
pytest's own plugins collect as pytest does by default. A literal
collect_ignore or collect_ignore_glob that matches the module stops its
collection.
"""

import ast
import fnmatch
import posixpath

from code_health.imports import Modules, loaded_imports, loaded_statements
from code_health.scopes import parents
from code_health.projects import Collection, Projects, PytestOptions
from code_health.projects import parents as folders_above
from code_health.symbols import SCOPES

UNITTEST_CASES = frozenset(
    {
        "unittest.TestCase",
        "unittest.case.TestCase",
        "unittest.IsolatedAsyncioTestCase",
        "unittest.async_case.IsolatedAsyncioTestCase",
        "django.test.SimpleTestCase",
        "django.test.TestCase",
        "django.test.TransactionTestCase",
        "django.test.LiveServerTestCase",
        "django.test.testcases.SimpleTestCase",
        "django.test.testcases.TestCase",
        "django.test.testcases.TransactionTestCase",
        "testtools.TestCase",
        "testtools.testcase.TestCase",
        "twisted.trial.unittest.TestCase",
        "twisted.trial.unittest.SynchronousTestCase",
        "absl.testing.absltest.TestCase",
        "asynctest.TestCase",
    }
)
UNITTEST_FILES = "test*.py"
COLLECTION_HOOKS = (
    "pytest_pycollect_makeitem",
    "pytest_pycollect_makemodule",
    "pytest_ignore_collect",
)
IGNORE_LISTS = ("collect_ignore", "collect_ignore_glob")
UNITTEST_LOADER = "load_tests"
HOOK_PREFIX = "pytest_"
AROUND_TESTS = {
    ast.Module: frozenset(
        {
            "pytestmark",
            "setup_module",
            "teardown_module",
            "setUpModule",
            "tearDownModule",
            "setup_function",
            "teardown_function",
        }
    ),
    ast.ClassDef: frozenset(
        {
            "pytestmark",
            "setup_class",
            "teardown_class",
            "setup_method",
            "teardown_method",
            "pytest_generate_tests",
        }
    ),
}
UNCONFIGURED = Collection(
    folder="",
    paths=[],
    files=PytestOptions().python_files.split(),
    classes=PytestOptions().python_classes.split(),
    functions=PytestOptions().python_functions.split(),
    plugins=[],
    disabled=[],
)
PYTEST_PLUGINS = frozenset(
    "mark main runner fixtures helpconfig python terminal debugging unittest "
    "capture skipping legacypath tmpdir monkeypatch recwarn pastebin assertion "
    "junitxml doctest cacheprovider freeze_support setuponly setupplan stepwise "
    "unraisableexception threadexception warnings logging reports python_path "
    "faulthandler subtests nose pytester pytester_assertions".split()
)


def literal_strings(statements: list[ast.stmt]) -> list[str] | None:
    """The strings one module-level assignment of a string, list or tuple of
    strings binds; None when the name is bound otherwise or changed later."""
    if len(statements) != 1 or isinstance(statements[0], ast.AugAssign):
        return None
    assigned = getattr(statements[0], "value", None)
    elements = (
        assigned.elts if isinstance(assigned, (ast.List, ast.Tuple)) else [assigned]
    )
    if not all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in elements
    ):
        return None
    return [element.value for element in elements]


def run_time_code(tree: ast.Module) -> str:
    """The exec or eval the module calls when it loads, which can bind any
    name."""
    for statement in loaded_statements(tree):
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") in (
            "exec",
            "eval",
        ):
            return call.func.id
    return ""


def binding_statements(tree: ast.Module) -> dict[str, list[ast.stmt]]:
    """The load-time statements that assign each module-level name or call a
    method on it."""
    found: dict[str, list[ast.stmt]] = {}
    for statement in loaded_statements(tree):
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
            if isinstance(call.func.value, ast.Name):
                found.setdefault(call.func.value.id, []).append(statement)
        if isinstance(statement, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = getattr(statement, "targets", None) or [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).append(statement)
    return found


def ignores(name: str, entries: list[str], relative: str) -> bool:
    """A conftest.py ignore list stops collection of the module at a path
    relative to the conftest.py: collect_ignore by path or directory,
    collect_ignore_glob by glob."""
    if name == "collect_ignore_glob":
        return any(fnmatch.fnmatch(relative, entry) for entry in entries)
    return any(
        relative == entry or relative.startswith(f"{entry}/") for entry in entries
    )


class TestCollection:
    """The test runner's collection decisions for the repository's modules."""

    def __init__(self, modules: Modules, projects: Projects):
        self.modules = modules
        self.projects = projects
        self.trees = modules.trees
        self.parent_maps: dict[str, dict[int, ast.AST]] = {}
        self.plugin_verdicts: dict[tuple[str, str], bool | str] = {}

    def scope(self, path: str, node: ast.AST) -> ast.AST:
        """The module, class or function whose body binds the node, past the
        branches and blocks between."""
        if path not in self.parent_maps:
            self.parent_maps[path] = parents(self.trees[path])
        around = self.parent_maps[path][id(node)]
        while not isinstance(around, SCOPES):
            around = self.parent_maps[path][id(around)]
        return around

    def collection(self, path: str) -> Collection | None:
        """The nearest pytest configuration's collection rules."""
        return next(
            (
                self.projects.collections[folder]
                for folder in [*folders_above(path), ""]
                if folder in self.projects.collections
            ),
            None,
        )

    def collected(self, path: str, function: ast.AST) -> bool | str:
        """Whether the test runner collects the function: pytest by its nearest
        configuration's python_files, python_classes and python_functions, or
        unittest as a test method of a TestCase subclass. In a module pytest
        collects, a collection hook or ignore list that a conftest.py beside or
        above the module binds, or that a plugin it names binds, decides
        collection in code; what leaves collection undecided is returned."""
        collection = self.collection(path)
        if collection is not None and collection.collects_file(path):
            verdict = self.conftest_collection(path)
            if verdict is not True:
                return verdict
        owner = self.scope(path, function)
        return self.declared_collection(path, owner, function.name, collection)

    def may_collect(self, path: str, owner: ast.AST, name: str) -> bool:
        """Whether a test runner may read the name bound in the module or
        class: as a test, unless collection rules it out, by the defaults
        pytest runs with where no configuration applies; as the marks, setup
        or teardown pytest reads around the tests it collects there; or, in a
        module, as a plugin hook or plugin list, which pytest finds by its
        prefix in whichever modules it loads as plugins, a conftest.py ignore
        list, or load_tests, which unittest calls in each module it loads."""
        if isinstance(owner, ast.Module):
            listing = posixpath.basename(path) == "conftest.py" and name in IGNORE_LISTS
            if listing or name.startswith(HOOK_PREFIX) or name == UNITTEST_LOADER:
                return True
        rules = self.collection(path) or UNCONFIGURED
        if rules.collects_file(path):
            verdict = self.conftest_collection(path)
            if verdict is not True:
                return verdict is not False
        if name in AROUND_TESTS.get(type(owner), ()):
            return self.holds_tests(path, owner, rules)
        return self.declared_collection(path, owner, name, rules)

    def conftest_collection(self, path: str) -> bool | str:
        """True when no conftest.py or plugin in the module's scope can change
        its collection, False when an ignore list excludes it, otherwise what
        decides it."""
        for conftest in self.conftests(path):
            relative = posixpath.relpath(path, posixpath.dirname(conftest) or ".")
            verdict = self.plugin_collection(conftest, relative)
            if verdict is not True:
                return verdict
        collection = self.collection(path) or UNCONFIGURED
        for plugin in collection.plugins:
            if plugin in collection.disabled:
                continue
            found = self.modules.absolute([None, *self.modules.roots], plugin, [])[0]
            verdict = self.enabled_plugin(plugin, found, "the pytest configuration")
            if verdict is not True:
                return verdict
        return True

    def plugin_collection(self, plugin: str, relative: str | None):
        """What a conftest.py, given the module's path relative to its folder,
        or a plugin module, given None, does to the module's collection."""
        key = (plugin, relative or "")
        if key not in self.plugin_verdicts:
            self.plugin_verdicts[key] = True
            self.plugin_verdicts[key] = self.plugin_effect(plugin, relative)
        return self.plugin_verdicts[key]

    def plugin_effect(self, plugin: str, relative: str | None):
        bound = self.hook_binding(plugin)
        if bound:
            return bound
        tree = self.trees[plugin]
        runner = run_time_code(tree)
        if runner:
            return f"the {runner}() call in {plugin}"
        statements = binding_statements(tree)
        for name in IGNORE_LISTS if relative is not None else ():
            if name in statements:
                entries = literal_strings(statements[name])
                if entries is None:
                    return f"the run-time {name} in {plugin}"
                if ignores(name, entries, relative):
                    return False
        if "pytest_plugins" not in statements:
            return True
        entries = literal_strings(statements["pytest_plugins"])
        if entries is None:
            return f"the run-time pytest_plugins in {plugin}"
        return self.named_plugins(plugin, entries)

    def hook_binding(self, plugin: str) -> str:
        """The collection hook the module binds, or the star import that does
        not resolve and may bind one."""
        exported = self.modules.exports(plugin)
        for hook in COLLECTION_HOOKS:
            if hook in exported:
                return f"the {hook} hook in {exported[hook]}"
        for node in loaded_imports(self.trees[plugin]):
            starred = isinstance(node, ast.ImportFrom) and any(
                alias.name == "*" for alias in node.names
            )
            if starred and self.modules.imported(plugin, node)[0][0] is None:
                star = "." * node.level + (node.module or "")
                return f"the unresolved star import of {star} in {plugin}"
        return ""

    def named_plugins(self, plugin: str, entries: list[str]):
        """What the plugins a module's pytest_plugins names do to the module's
        collection."""
        collection = self.collection(plugin)
        disabled = collection.disabled if collection is not None else []
        for entry in entries:
            if entry in disabled:
                continue
            found = self.modules.absolute(self.modules.bases(plugin), entry, [])[0]
            verdict = self.enabled_plugin(entry, found, plugin)
            if verdict is not True:
                return verdict
        return True

    def enabled_plugin(self, name: str, found: str | None, namer: str):
        """What an enabled plugin, given the repository file it resolves to,
        does to the module's collection: pytest's own collects as by default;
        one whose code is not read leaves it undecided."""
        if name in PYTEST_PLUGINS or name.startswith("_pytest."):
            return True
        if found:
            return self.plugin_collection(found, None)
        if name.split(".")[0] in self.modules.packages:
            return f"the unresolved plugin {name} {namer} names"
        return f"the external plugin {name} {namer} names, whose hooks are not read"

    def holds_tests(
        self, path: str, owner: ast.AST, collection: Collection | None
    ) -> bool:
        """Whether pytest collects the test functions bound in the module or
        the class."""
        if collection is None or not collection.collects_file(path):
            return False
        if isinstance(owner, ast.Module):
            return True
        return (
            isinstance(owner, ast.ClassDef)
            and isinstance(self.scope(path, owner), ast.Module)
            and Collection.matches(collection.classes, owner.name)
            and not any(
                isinstance(method, ast.FunctionDef) and method.name == "__init__"
                for method in owner.body
            )
        )

    def declared_collection(
        self, path: str, owner: ast.AST, name: str, collection: Collection | None
    ) -> bool:
        if isinstance(owner, ast.ClassDef) and isinstance(
            self.scope(path, owner), ast.Module
        ):
            imports = {
                alias.asname or alias.name: f"{node.module}.{alias.name}"
                for node in self.trees[path].body
                if isinstance(node, ast.ImportFrom) and node.module
                for alias in node.names
            }
            case = any(
                imports.get(ast.unparse(base), ast.unparse(base)) in UNITTEST_CASES
                for base in owner.bases
            )
            if case and name.startswith("test"):
                return self.collects_case(path)
        return self.holds_tests(path, owner, collection) and Collection.matches(
            collection.functions, name
        )

    def collects_case(self, path: str) -> bool:
        """Whether unittest test methods in the file are collected: by the
        nearest pytest configuration's python_files, else by unittest's own
        file pattern."""
        collection = self.collection(path)
        if collection is not None:
            return collection.collects_file(path)
        return fnmatch.fnmatchcase(posixpath.basename(path), UNITTEST_FILES)

    def collects_tree(self, path: str) -> bool:
        """Whether the file lies in a tree the test runner collects by default:
        the nearest pytest configuration's testpaths, or anywhere without
        one."""
        collection = self.collection(path)
        return collection is None or collection.collects_tree(path)

    def conftests(self, path: str) -> list[str]:
        """The conftest.py files whose hooks and fixtures apply to the module,
        nearest first."""
        return [
            conftest
            for folder in [*folders_above(path), ""]
            if (conftest := posixpath.join(folder, "conftest.py")) in self.trees
        ]
