"""Whole-repository Python index of symbols, imports, exports and string
references.
"""

import ast
import posixpath
import re
from collections import defaultdict

from code_health.consumers import NameConsumers
from code_health.distribution import Distributions
from code_health.forwarding import documented
from code_health.hierarchy import Hierarchy
from code_health.imports import (
    Modules,
    loaded_imports,
    loaded_statements,
    module_path,
)
from code_health.scopes import parents
from code_health.symbols import FUNCTIONS, OUTSIDE, Symbols
from code_health.visibility import all_listing, assigned

SOURCE_ROOTS = ("", "src", "python", "lib")


MODULE_SCOPE = (*FUNCTIONS, ast.Lambda, ast.ClassDef)


def defined_names(node: ast.stmt) -> set[str]:
    """Names a definition or an assignment statement binds."""
    if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return {
            name.id
            for target in assigned(node)
            for name in ast.walk(target)
            if isinstance(name, ast.Name)
        }
    return set()


def module_bound_names(tree: ast.Module) -> set[str]:
    """Names the module binds at load: its imports, definitions and assignments."""
    bound = set()
    for node in loaded_statements(tree, MODULE_SCOPE):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update(
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            )
        else:
            bound |= defined_names(node)
    return bound


def spelled_source(path: str, node, alias: ast.alias) -> str:
    """An import as its statement spells it, a relative one from its folder."""
    if isinstance(node, ast.Import):
        return alias.name if alias.asname else alias.name.split(".")[0]
    folder = f"{posixpath.dirname(path)}:" if node.level else ""
    return f"{folder}{'.' * node.level}{node.module or ''}.{alias.name}"


def import_sources(path: str, tree: ast.Module, modules: Modules) -> dict[str, str]:
    """What each name an import binds at load stands for: the repository file
    and name it resolves to, otherwise the statement's own spelling, and every
    one of them where several imports bind the name. A name a definition or an
    assignment also binds has no import for its source and is left out."""
    found: dict[str, set[str]] = defaultdict(set)
    for node in loaded_statements(tree, MODULE_SCOPE):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            for name in defined_names(node):
                found[name].add("")
            continue
        imported = modules.imported(path, node)
        for (module, name), alias in zip(imported, node.names, strict=True):
            package = isinstance(node, ast.Import) and not alias.asname
            if module is None or (package and "." in alias.name):
                source = spelled_source(path, node, alias)
            else:
                source = f"{module}:{name}"
            found[alias.asname or alias.name.split(".")[0]].add(source)
    return {
        name: " or ".join(sorted(sources))
        for name, sources in found.items()
        if "" not in sources
    }


def root_name(node: ast.Attribute) -> str | None:
    """The name an attribute chain starts from: a for a.b.c."""
    current = node.value
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def split_reference(text: str) -> list[str] | None:
    """The identifiers of a dotted reference string, None when the string is
    not one: a.b.c, an entry point a.b:c, or a bare identifier."""
    parts = re.split(r"[.:]", text)
    return parts if all(part.isidentifier() for part in parts) else None


class Repository:
    """Every reference in the repository, indexed by the name it uses."""

    def __init__(
        self, trees: dict[str, ast.Module], shipped: Distributions, modules: Modules
    ):
        self.trees = trees
        self.shipped = shipped
        self.parent_of = {path: parents(tree) for path, tree in trees.items()}
        self.modules = modules
        self.symbols = Symbols(trees, self.parent_of, self.modules)
        self.hierarchy = Hierarchy(trees, self.symbols)
        self.consumers = NameConsumers(
            self.symbols, self.hierarchy.reaching, self.parent_of
        )
        self.bindings = {path: module_bound_names(tree) for path, tree in trees.items()}
        self.sources = {
            path: import_sources(path, tree, modules) for path, tree in trees.items()
        }
        self.listings = {path: all_listing(tree) for path, tree in trees.items()}
        self.names: dict[str, dict[str, list[ast.Name]]] = {}
        self.attributes: dict[str, list[tuple[str, ast.Attribute]]] = defaultdict(list)
        self.strings: dict[str, list[tuple[str, ast.Constant]]] = defaultdict(list)
        self.keyword_names: dict[str, list[tuple[str, ast.keyword, ast.Call]]] = (
            defaultdict(list)
        )
        self.aliases: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        self.module_files: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.docstrings = {
            id(node.body[0].value)
            for tree in trees.values()
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, *FUNCTIONS))
            and node.body
            and documented(node)
        }
        indexers = {
            ast.Name: self.index_name,
            ast.Attribute: self.index_attribute,
            ast.Constant: self.index_string,
            ast.Import: self.index_import,
            ast.ImportFrom: self.index_import,
            ast.Call: self.index_patch_multiple,
        }
        for path, tree in trees.items():
            self.names[path] = defaultdict(list)
            for node in ast.walk(tree):
                if indexer := indexers.get(type(node)):
                    indexer(path, node)
        self.exports, self.uncertain_exports = self.exported_names()

    def index_name(self, path: str, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.names[path][node.id].append(node)

    def index_attribute(self, path: str, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.attributes[node.attr].append((path, node))

    def index_string(self, path: str, node: ast.Constant) -> None:
        """A string that may name a function: a dotted reference, or the
        attribute-name argument of a call spelled like a name consumer."""
        if not isinstance(node.value, str) or id(node) in self.docstrings:
            return
        parts = split_reference(node.value)
        if parts is None:
            return
        if len(parts) > 1 or self.consumers.consumer(path, node) is not None:
            self.strings[parts[-1]].append((path, node))

    def index_patch_multiple(self, path: str, node: ast.Call) -> None:
        """The keyword names of a call that may be patch.multiple: one spelled
        so, or one whose callee resolves to it through an alias."""
        if not (node.keywords and node.args):
            return
        if self.consumers.multiple(path, node) is None:
            return
        for keyword in node.keywords:
            if keyword.arg:
                self.keyword_names[keyword.arg].append((path, keyword, node))

    def index_import(self, path: str, node) -> None:
        """The names an import binds: a module alias to the files it can be, and a
        function name to the file that defines it."""
        known = self.modules.known
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name if alias.asname else alias.name.split(".")[0]
                self.module_files[path][alias.asname or dotted] |= {
                    found
                    for base in self.modules.bases(path)
                    if base is not None and (found := module_path(base, dotted, known))
                }
            return
        for (module, name), alias in zip(
            self.modules.imported(path, node), node.names, strict=True
        ):
            bound = alias.asname or alias.name
            if module is None:
                continue
            if not name:
                self.module_files[path][bound].add(module)
            elif name == "*":
                for star, defining in self.modules.exports(module).items():
                    self.aliases[(self.modules.origin(defining, star), star)].append(
                        (path, star)
                    )
            else:
                self.aliases[(self.modules.origin(module, name), name)].append(
                    (path, bound)
                )

    def module_aliases(self, path: str, defining: str) -> set[str]:
        """The names in a file that bind the defining module or a package above it."""
        targets = {defining}
        folder = defining.rsplit("/", 1)[0] if "/" in defining else ""
        while folder:
            targets.add(f"{folder}/__init__.py")
            folder = folder.rsplit("/", 1)[0] if "/" in folder else ""
        return {
            alias for alias, files in self.module_files[path].items() if files & targets
        }

    def exported_names(self) -> tuple[set, set]:
        """(file, name) pairs that __all__ or a package initializer exports,
        each at the module that defines the name, and the pairs an undecided
        listing may or may not export, followed through re-exports and star
        imports to their defining modules."""
        found: set[tuple[str, str]] = set()
        uncertain: set[tuple[str, str]] = set()
        for path in self.trees:
            listed, dynamic = self.listings[path]
            sources, undecided = self.bound_sources(path)
            bound = self.bindings[path] | set(sources)

            def source(name: str) -> tuple[str, str]:
                return sources[name] if name in sources else (path, name)

            if dynamic:
                uncertain |= {source(name) for name in bound}
                continue
            initializer = path.endswith("__init__.py")
            if listed is not None:
                names = listed
            elif initializer:
                names = {name for name in bound if not name.startswith("_")}
            else:
                names = set()
            uncertain |= {
                source(name) for name in undecided if initializer or name in names
            }
            found |= {source(name) for name in names if name not in undecided}
        return found, uncertain

    def bound_sources(self, path: str) -> tuple[dict, set[str]]:
        """Each name an import binds in a module with the file and name it comes
        from, and the names a star import takes from an undecided listing."""
        sources: dict[str, tuple[str, str]] = {}
        undecided: set[str] = set()
        for node in loaded_imports(self.trees[path]):
            for (module, name), alias in zip(
                self.modules.imported(path, node), node.names, strict=True
            ):
                if module is None or not name:
                    continue
                if name != "*":
                    sources[alias.asname or alias.name] = (
                        self.modules.origin(module, name),
                        name,
                    )
                    continue
                starred, starred_dynamic = self.listings[module]
                for star, defining in self.modules.exports(module).items():
                    if starred_dynamic:
                        undecided.add(star)
                    elif not (
                        (starred is None and not star.startswith("_"))
                        or (starred is not None and star in starred)
                    ):
                        continue
                    sources[star] = (self.modules.origin(defining, star), star)
        return sources, undecided

    def dotted(self, parts: list[str]) -> list | None:
        """What a dotted path resolves to: the longest prefix that is a
        repository module, then each attribute after it. A path whose first part
        is no repository module or package names something outside; one that
        starts inside the repository and cannot be followed is None."""
        roots = list(dict.fromkeys([*self.shipped.import_roots, *SOURCE_ROOTS]))
        resolved = self.modules.dotted_module(parts, roots, len(parts) - 1)
        if resolved is not None:
            module, remainder = resolved
            resolution: list | None = [("module", module)]
            for part in remainder:
                resolution = self.symbols.member(resolution, part)
                if resolution is None:
                    return None
            return resolution
        inside_repository = any(
            module_path(base, parts[0], self.modules.known) for base in roots
        )
        return None if inside_repository else [OUTSIDE]
