"""Import cycles: strongly connected groups of the repository's own modules,
linked by the imports that run when a module loads. Every group is reported,
whatever directory or file name its modules have.

An absolute import searches what Python searches: the directories the module's
top-level sys.path.insert calls name with paths built from __file__, a script's
own directory, then the standard library, then the source roots the
repository's project declarations name (code_health.projects), nearest to the
importing file first, so each project of a monorepo resolves its own packages,
then the directory above the module's outermost package, which is on the
import path whenever the module loads under its package name. A name the
standard library provides is never a repository module. The repository's own
top-level names are the top-level modules its roots hold and its outermost
packages, directories with an __init__.py, wherever they sit. An absolute
import of one of them that no searched root resolves leaves the graph
incomplete and is reported at its site; any other name that resolves to no
root is a third-party module, even when a plain module elsewhere shares it. A
name imported through a package that re-exports it links to the module that
defines it. Function bodies, type-checking blocks and
script-only main guards do not run when imported and do not count, nor does a
branch of a module-level if whose condition the module's own constant
assignments decide; when the module rewrites its globals through globals() in
a way the AST cannot fold, the guarded imports are reported as incomplete.
"""

import ast
import posixpath
import re
import sys
from dataclasses import dataclass

from code_health.folding import ModuleStrings, folded
from code_health.symbols import module_path

STANDARD_LIBRARY = sys.stdlib_module_names
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
LOADED_SKIP = (*FUNCTIONS, ast.ClassDef)
BLOCKS = (ast.stmt, ast.excepthandler, ast.match_case)
DEFINITIONS = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Assign,
    ast.AnnAssign,
)


def not_imported(test: ast.expr) -> bool:
    if getattr(test, "id", getattr(test, "attr", "")) == "TYPE_CHECKING":
        return True
    return main_guard(test)


def main_guard(test: ast.expr) -> bool:
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    ):
        return False
    operands = (test.left, test.comparators[0])
    return any(
        isinstance(part, ast.Name) and part.id == "__name__" for part in operands
    ) and any(
        isinstance(part, ast.Constant) and part.value == "__main__" for part in operands
    )


UNDECIDED = object()


def globals_write(target: ast.expr) -> ast.expr | None:
    """The key of a globals()[key] assignment target."""
    if (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Call)
        and isinstance(target.value.func, ast.Name)
        and target.value.func.id == "globals"
    ):
        return target.slice
    return None


def assigned_constants(statement, constants: dict[str, object]) -> bool:
    """Record the constant, or UNDECIDED, each target of a module-level
    assignment or value-bearing annotated assignment receives; whether a
    globals() key could not be folded."""
    constant = (
        statement.value.value
        if isinstance(statement.value, ast.Constant)
        else UNDECIDED
    )
    unfolded = False
    for target in getattr(statement, "targets", None) or [statement.target]:
        if isinstance(target, ast.Name):
            constants[target.id] = constant
        elif (key := globals_write(target)) is not None:
            name = folded(key)
            unfolded |= name is None
            if name is not None:
                constants[name] = constant
    return unfolded


def guard_verdict(test: ast.expr, constants: dict[str, object], rewritten: bool):
    """The verdict of one module-level if condition that is a name, its
    negation or a constant: True or False when the latest constant binding of
    the name decides it, UNDECIDED when the name was rebound by a statement
    that binds no constant or rewritten through globals() with a key the AST
    cannot fold, None when the condition is something else."""
    negated = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, negated = test.operand, True
    if isinstance(test, ast.Constant):
        return bool(test.value) != negated
    if isinstance(test, ast.Name) and test.id in constants:
        bound = constants[test.id]
        if rewritten or bound is UNDECIDED:
            return UNDECIDED
        return bool(bound) != negated
    return None


def guard_verdicts(tree: ast.Module) -> dict[int, object]:
    """Each module-level if's verdict from the constant bindings before it; an
    annotation without a value binds nothing."""
    constants: dict[str, object] = {}
    rewritten = False
    verdicts: dict[int, object] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) or (
            isinstance(statement, ast.AnnAssign) and statement.value is not None
        ):
            rewritten |= assigned_constants(statement, constants)
        elif isinstance(statement, ast.If):
            verdict = guard_verdict(statement.test, constants, rewritten)
            if verdict is not None:
                verdicts[id(statement)] = verdict
        elif isinstance(statement, (*FUNCTIONS[:2], ast.ClassDef, ast.AugAssign)):
            for name in bound_names(statement):
                if name in constants:
                    constants[name] = UNDECIDED
    return verdicts


def loaded_statements(tree: ast.Module, skip: tuple = FUNCTIONS, undecided=None):
    """Statements that run when the module loads: outside function bodies and
    if TYPE_CHECKING blocks, outside a module-level branch its condition rules
    out, and outside any other skipped node. A branch whose condition cannot be
    decided is entered, unless undecided collects such ifs instead."""
    verdicts = guard_verdicts(tree)
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        yield node
        verdict = verdicts.get(id(node))
        if isinstance(node, ast.If) and not_imported(node.test):
            pending.extend(node.orelse)
        elif isinstance(node, ast.If) and verdict is not None:
            if verdict is UNDECIDED and undecided is not None:
                undecided.append(node)
            elif verdict is UNDECIDED:
                pending.extend([*node.body, *node.orelse])
            else:
                pending.extend(node.body if verdict else node.orelse)
        elif not isinstance(node, skip):
            pending.extend(
                child
                for child in ast.iter_child_nodes(node)
                if isinstance(child, BLOCKS)
            )


def path_entries(path: str, tree: ast.Module) -> tuple[list[str], list[str]]:
    """The repository directories the module's top-level statements put at
    the front and at the end of sys.path."""
    strings = ModuleStrings(path, list(loaded_statements(tree, LOADED_SKIP)))
    inserted: list[str] = []
    appended: list[str] = []
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
        ):
            continue
        call = statement.value
        callee = ast.unparse(call.func)
        if callee == "sys.path.insert" and len(call.args) == 2:
            folder = strings.repository_path(call.args[1])
            inserted[:0] = [] if folder is None else [folder]
        elif callee == "sys.path.append" and len(call.args) == 1:
            folder = strings.repository_path(call.args[0])
            appended += [] if folder is None else [folder]
    return inserted, appended


def loaded_imports(tree: ast.Module):
    for node in loaded_statements(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def script(path: str, known: set[str]) -> bool:
    """A file with no package initializer beside or above it runs as a script,
    with its own directory first on the import path."""
    folder = posixpath.dirname(path)
    while folder:
        if posixpath.join(folder, "__init__.py") in known:
            return False
        folder = posixpath.dirname(folder)
    return True


def runs_as_script(tree: ast.Module) -> bool:
    """The module guards code to run when it is executed directly."""
    return any(
        isinstance(statement, ast.If) and main_guard(statement.test)
        for statement in tree.body
    )


def table_module(written: ast.expr) -> str:
    """The module a lazy-import table entry names: a string, or the second
    element of a (package, module) pair."""
    if isinstance(written, ast.Tuple) and len(written.elts) == 2:
        written = written.elts[1]
    if isinstance(written, ast.Constant) and isinstance(written.value, str):
        return written.value
    return ""


def module_name(path: str, known: set[str]) -> str:
    """The dotted module a file defines, counted from the directory above its
    outermost package, through any directory without an initializer below that
    package, which Python imports as a namespace package; a stub defines the
    same module as its source."""
    parts = re.sub(r"\.pyi?$", "", path).split("/")
    if parts[-1] == "__init__":
        parts.pop()
    folder = posixpath.dirname(path)
    base = folder
    while folder:
        if {f"{folder}/__init__.py", f"{folder}/__init__.pyi"} & known:
            base = posixpath.dirname(folder)
        folder = posixpath.dirname(folder)
    return ".".join(parts[len(base.split("/")) if base else 0 :])


def package_base(path: str, dotted: str) -> str:
    """The directory above the outermost package of the file that defines the
    dotted module, which must be on the import path for the file to load
    under that name."""
    base = path
    for _ in dotted.split("."):
        base = posixpath.dirname(base)
    return base


def imported_tops(imports) -> set[str]:
    """The top-level names absolute import statements name."""
    tops = set()
    for node in imports:
        if isinstance(node, ast.Import):
            tops.update(alias.name.split(".")[0] for alias in node.names)
        elif not node.level and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def script_roots(trees: dict[str, ast.Module]) -> set[str]:
    """The directories Python puts first on the import path for code the
    repository runs: a script's own directory when the script imports a
    package that sits there, and the base directory of a conftest.py, the
    directory above its outermost package, which pytest prepends before
    loading it."""
    known = set(trees)
    roots = set()
    for path, tree in trees.items():
        if posixpath.basename(path) == "conftest.py":
            roots.add(package_base(path, module_name(path, known)))
        elif script(path, known):
            folder = posixpath.dirname(path)
            if any(
                module_path(folder, top, known)
                == posixpath.join(folder, top, "__init__.py")
                for top in imported_tops(loaded_imports(tree))
            ):
                roots.add(folder)
    return roots


def bound_names(node: ast.stmt) -> list[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [
        name.id
        for target in targets
        for name in ast.walk(target)
        if isinstance(name, ast.Name)
    ]


class Modules:
    """The repository's modules, and the file that defines each name a package
    exports, so an import through a package initializer that re-exports a
    submodule's name depends on that submodule rather than on the whole package.
    A plain module that re-exports a name is itself the dependency."""

    def __init__(
        self, trees: dict[str, ast.Module], roots: set[str], aliases: dict | None = None
    ):
        self.trees = trees
        self.aliases = aliases or {}
        self.known = set(trees) | set(self.aliases)
        self.roots = sorted(roots)
        self.exported: dict[str, dict[str, str]] = {}
        self.searched: dict[str, list[str | None]] = {}
        self.packages = {
            module_name(path, self.known).split(".")[0]
            for path in trees
            if posixpath.basename(path) == "__init__.py"
        } | self.root_modules()
        self.unresolved: dict[str, set[tuple[int, str]]] = {}

    def root_modules(self) -> set[str]:
        """The top-level modules the roots hold."""
        found = set()
        for root in self.roots:
            prefix = root + "/" if root else ""
            for path in self.known:
                if path.startswith(prefix):
                    top = re.sub(r"\.pyi?$", "", path[len(prefix) :]).split("/")[0]
                    if module_path(root, top, self.known):
                        found.add(top)
        return found

    def bases(self, path: str) -> list[str | None]:
        """The directories an absolute import from the file searches, in order;
        None stands for the standard library."""
        if path in self.searched:
            return self.searched[path]
        folder = posixpath.dirname(path)
        inserted, appended = path_entries(path, self.trees[path])
        key = path if inserted or appended else folder
        if key not in self.searched:
            nearest = sorted(
                self.roots,
                key=lambda root: (
                    -len(posixpath.commonpath([root, folder])) if root and folder else 0
                ),
            )
            first = [folder] if script(path, self.known) else []
            base = package_base(path, module_name(path, self.known))
            self.searched[key] = [*inserted, *first, None, *nearest, base, *appended]
        self.searched[path] = self.searched[key]
        return self.searched[key]

    def name(self, path: str) -> str:
        """The dotted module a file in a package defines: counted from the
        nearest declared source root strictly above its outermost package,
        through the namespace directories between, else as module_name counts
        it."""
        plain = module_name(path, self.known)
        base = package_base(path, plain)
        if base == posixpath.dirname(path) or base in self.roots:
            return plain
        enclosing = [
            root for root in self.roots if root and base.startswith(f"{root}/")
        ]
        if not enclosing:
            return plain
        root = max(enclosing, key=len)
        stem = re.sub(r"\.pyi?$", "", path)[len(root) + 1 :]
        return ".".join(stem.removesuffix("/__init__").split("/"))

    def search_roots(self) -> list[str]:
        """Every directory an import in the repository searches: the declared
        source roots, the directory above each package, and each directory a
        file puts on the import path."""
        searched = set(self.roots)
        for path, tree in self.trees.items():
            inserted, appended = path_entries(path, tree)
            searched.update(inserted, appended)
            if not script(path, self.known):
                searched.add(package_base(path, module_name(path, self.known)))
        return sorted(searched)

    def located(self, base: str, dotted: str) -> str | None:
        """The file defining a dotted module under a base directory; a file
        reached through a tracked symlink to a repository directory is the
        file the link points at."""
        stem = posixpath.join(base, *dotted.split(".")) if dotted else base
        for found in (f"{stem}.py", posixpath.join(stem, "__init__.py")):
            if found in self.known:
                return self.aliases.get(found, found)
        return None

    def dotted_module(
        self, parts: list[str], roots: list[str], limit: int
    ) -> tuple[str, list[str]] | None:
        """The repository file of the longest module prefix of parts, up to
        limit components, under roots, and the attribute names after it."""
        for length in range(min(limit, len(parts)), 0, -1):
            dotted_name = ".".join(parts[:length])
            for base in roots:
                found = self.located(base, dotted_name)
                if found is not None:
                    return found, parts[length:]
        return None

    def origin(self, module: str, name: str) -> str:
        """The file that defines a name, following re-exports through any module."""
        seen = set()
        while module not in seen:
            seen.add(module)
            target = self.exports(module).get(name)
            if target is None or target == module:
                return module
            module = target
        return module

    def absolute(self, bases: list[str | None], dotted: str, names: list[str]):
        """Searching the bases in order, the file defining the dotted module in
        the first base that holds it or one of the imported names, and the file
        of each imported name that is itself a submodule; nothing for the
        standard library or a module no base holds."""
        prefix = dotted + "." if dotted else ""
        for base in bases:
            if base is None:
                if dotted.split(".")[0] in STANDARD_LIBRARY:
                    break
                continue
            module = self.located(base, dotted)
            subs = [self.located(base, prefix + name) for name in names]
            if module or any(subs):
                return module, subs
        return None, [None] * len(names)

    def imported(self, path: str, node) -> list[tuple[str, str]]:
        """The module file and the name, empty for a whole module, of each name
        an import statement binds."""
        if isinstance(node, ast.Import):
            found = []
            for alias in node.names:
                module = self.absolute(self.bases(path), alias.name, [])[0]
                if module is None:
                    self.unresolvable(path, node, alias.name)
                found.append((module, ""))
            return found
        names = [alias.name for alias in node.names]
        if node.level:
            base = posixpath.dirname(path)
            for _ in range(node.level - 1):
                base = posixpath.dirname(base)
            prefix = node.module + "." if node.module else ""
            module = self.located(base, node.module or "")
            subs = [self.located(base, prefix + name) for name in names]
        else:
            module, subs = self.absolute(self.bases(path), node.module or "", names)
        if not (module or any(subs)):
            if not node.level:
                self.unresolvable(path, node, node.module or "")
            return [(None, "")] * len(names)
        return [
            (sub, "") if sub else (module, alias.name)
            for sub, alias in zip(subs, node.names, strict=True)
        ]

    def unresolvable(self, path: str, node, written: str) -> None:
        """Record an absolute import of one of the repository's own top-level
        packages that no searched root resolves."""
        top = written.split(".")[0]
        if top in self.packages and top not in STANDARD_LIBRARY:
            self.unresolved.setdefault(path, set()).add((node.lineno, written))

    def incomplete(self, loaded: dict[str, set[int]]) -> dict[str, list[str]]:
        """For each module with load-time repository imports, at the given
        lines, that no root resolves, one note naming them."""
        found = {}
        for path, sites in sorted(self.unresolved.items()):
            listed = [
                f"line {line} {written}"
                for line, written in sorted(sites)
                if line in loaded.get(path, set())
            ]
            if listed:
                found[path] = [
                    f"{', '.join(listed)} name the repository's own packages but "
                    "no searched root holds them"
                ]
        return found

    def exports(self, path: str) -> dict[str, str]:
        """Each name the module binds at load, and the file that defines it."""
        if path in self.exported:
            return self.exported[path]
        names: dict[str, str] = {}
        self.exported[path] = names
        for node in loaded_imports(self.trees[path]):
            names.update(self.import_bindings(path, node))
        for node in loaded_statements(self.trees[path], (*FUNCTIONS, ast.ClassDef)):
            if isinstance(node, DEFINITIONS):
                names.update(dict.fromkeys(bound_names(node), path))
        for name, module in self.lazy_exports(path).items():
            names.setdefault(name, self.defining(module, name))
        return names

    def lazy_exports(self, path: str) -> dict[str, str]:
        """The names a module-level __getattr__ serves from a literal table of
        name to module, or to a (package, module) pair whose module is read
        against the table's own package, and the repository file of each
        module; a table entry naming a module no searched root holds is left
        unresolved."""
        tree = self.trees[path]
        if "__getattr__" not in {
            name
            for node in tree.body
            if isinstance(node, DEFINITIONS)
            for name in bound_names(node)
        }:
            return {}
        found: dict[str, str] = {}
        for node in loaded_statements(tree, (*FUNCTIONS, ast.ClassDef)):
            table = getattr(node, "value", None)
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(
                table, ast.Dict
            ):
                continue
            for key, written in zip(table.keys, table.values, strict=True):
                dotted = table_module(written)
                if not (isinstance(key, ast.Constant) and dotted):
                    continue
                module = self.lazy_module(path, dotted)
                if module is not None:
                    found[key.value] = module
        return found

    def lazy_module(self, path: str, dotted: str) -> str | None:
        """The repository file of a module a lazy-import table names, relative
        to the package of the module holding the table when it starts with a
        dot."""
        if not dotted.startswith("."):
            return self.absolute(self.bases(path), dotted, [])[0]
        level = len(dotted) - len(dotted.lstrip("."))
        base = posixpath.dirname(path)
        for _ in range(level - 1):
            base = posixpath.dirname(base)
        return self.located(base, dotted[level:])

    def import_bindings(self, path: str, node) -> dict[str, str]:
        """The names one import statement binds, and the file behind each."""
        names: dict[str, str] = {}
        imported = self.imported(path, node)
        for (module, name), alias in zip(imported, node.names, strict=True):
            if module is None:
                continue
            if name == "*":
                names.update(
                    self.exports(module)
                    if module.endswith("__init__.py")
                    else dict.fromkeys(self.exports(module), module)
                )
            elif name:
                names[alias.asname or name] = self.defining(module, name)
            elif isinstance(node, ast.ImportFrom):
                names[alias.asname or alias.name] = module
        return names

    def defining(self, module: str, name: str) -> str:
        if not module.endswith("__init__.py"):
            return module
        return self.exports(module).get(name, module)

    def targets(self, path: str, node) -> set[str]:
        """The repository files an import statement depends on."""
        found = set()
        for module, name in self.imported(path, node):
            if module is None:
                continue
            if name == "*" and module.endswith("__init__.py"):
                found.update(self.exports(module).values() or [module])
            else:
                found.add(self.defining(module, name) if name else module)
        return found


def finish_order(graph: dict[str, set[str]]) -> list[str]:
    """Nodes in the order an iterative depth-first search finishes them."""
    seen: set[str] = set()
    order = []
    for start in sorted(graph):
        if start in seen:
            continue
        seen.add(start)
        work = [(start, iter(sorted(graph[start])))]
        while work:
            node, children = work[-1]
            child = next((c for c in children if c not in seen), None)
            if child is None:
                order.append(node)
                work.pop()
            else:
                seen.add(child)
                work.append((child, iter(sorted(graph[child]))))
    return order


def strong_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Kosaraju's strongly connected components of more than one node."""
    reverse: dict[str, set[str]] = {node: set() for node in graph}
    for node, children in graph.items():
        for child in children:
            reverse[child].add(node)
    seen: set[str] = set()
    found = []
    for start in reversed(finish_order(graph)):
        if start in seen:
            continue
        seen.add(start)
        component, pending = [], [start]
        while pending:
            node = pending.pop()
            component.append(node)
            fresh = reverse[node] - seen
            seen |= fresh
            pending.extend(fresh)
        if len(component) > 1:
            found.append(sorted(component))
    return found


@dataclass
class ImportGraph:
    """The import cycles, and the incomplete analysis by the file it stands
    in."""

    cycles: list[str]
    incomplete: dict[str, list[str]]


def import_cycles(modules: Modules) -> ImportGraph:
    """One finding per group of modules that import each other, and a note
    for each module whose load-time imports the graph cannot settle: a guard
    whose condition cannot be decided, or repository imports no root
    resolves."""
    graph: dict[str, set[str]] = {}
    loaded: dict[str, set[int]] = {}
    incomplete: dict[str, list[str]] = {}
    for path, tree in modules.trees.items():
        undecided: list[ast.If] = []
        imports = [
            node
            for node in loaded_statements(tree, FUNCTIONS, undecided)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        loaded[path] = {node.lineno for node in imports}
        graph[path] = {
            target
            for node in imports
            for target in modules.targets(path, node)
            if target != path
        }
        guards = [
            f"line {node.lineno}: the guard {ast.unparse(node.test)} depends on a "
            "globals() rewrite the AST cannot fold"
            for node in undecided
            if any(
                isinstance(part, (ast.Import, ast.ImportFrom))
                for part in ast.walk(node)
            )
        ]
        if guards:
            incomplete[path] = guards
    for path, notes in modules.incomplete(loaded).items():
        incomplete.setdefault(path, []).extend(notes)
    cycles = [
        " → ".join(
            member.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
            for member in component
        )
        for component in sorted(strong_components(graph))
    ]
    return ImportGraph(cycles, dict(sorted(incomplete.items())))
