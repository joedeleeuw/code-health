"""Unused code: a function, class or variable, or a member of a class, that a
measured file declares and no tracked file of the repository reads. A
member is a method, property, class variable, nested class or assigned
attribute. A stub declares another file's names and holds no code, so it
declares none.

The resolver says what each name refers to, so a use is a reference to this
declaration, not another occurrence of the same spelling. What counts as used
without a reference is decided by facts the resolver or the syntax tree gives:

A name Python or a framework calls itself, one that starts and ends with an
underscore (a dunder, a display hook such as _repr_html_, an Enum's _missing_),
and a name the module's __all__ lists are used. So are each object on the path
an entry point of the project loads ([project.scripts], gui-scripts and
entry-points in pyproject.toml), or on a dotted or colon reference into the
repository's own modules in a Python string literal or a tracked configuration,
data, template, script, container or process file, and for a public declaration
of a module that ships as public or whose shipping does not resolve, in tracked
prose documentation (code_health.outside_python); every module-level name of a
Sphinx
configuration, a conf.py beside the index document Sphinx reads with it, every
name of a module a type checker alone reads, one under a file or directory it
is told to read in which nothing runs any module (code_health.specimens), and
what a Protocol declares, which states what the classes that satisfy it must
have. So is a name a test
runner may read: a test it collects where the name is bound or in a class of
the repository that inherits it, the marks, setup and teardown it reads around
those, a plugin hook and a conftest.py ignore list; and a class it may collect
a test from, its own or an inherited one. A definition handed to a decorator is
used, unless every decorator is one of the standard library's that returns the
definition to its own name (INERT_DECORATORS) or a property's own setter,
getter or deleter. Classes are related when one derives from the other or a
class of the repository derives from both. A class is exposed to code outside
the repository when it has a base declared outside, other than the ones that
call no member they do not declare (INERT_BASES, object and the built-in
exceptions), a decorator that is not inert, a metaclass, or a keyword that a
base outside the standard library may receive; every member of a class related
to an exposed one is used, and of a class nested in one. A base is followed
through each variable of the repository that only names another. Such code may
also find a class nothing refers to: a class is used when it or an ancestor is
built by such a decorator, metaclass or keyword, or has a base declared outside
the standard library, or derives from unittest's TestCase. The repository's own
code finds one too: a class is used when an ancestor defines __init_subclass__,
which receives it, or has its __subclasses__ read, on the class by name or on
the receiver of one of its own methods, and a read of __subclasses__ on
anything else may list any. The fields of a dataclass are read by the methods
the decorator writes for it (comparison, representation, hashing) and by
asdict, so they are used.

Every binding of one name in one module, class or function is one declaration,
an import of the same name included, and the same member declared by
related classes is one member: a use of any is a use of all, so a definition
on a branch pyright does not analyse is used when its twin on the other branch
is, and what makes one binding or one related class's member used without a
reference makes them all used. A class in code pyright does not analyse, a
branch for another platform or version, stands in for the one the other branch
binds, so its members are not judged. An assigned attribute is the declaration
pyright resolves it to, a variable or field of the class, a name its __slots__
lists or another assignment, and has that declaration's verdict. One assigned
on an object declared outside the repository or in a stub, or on one pyright
cannot place, belongs to that object's owner. A use that lands in a stub
beside a measured module, which pyright answers with in place of the source,
is a use of the declarations of that name in the module. A declaration in
code pyright does not analyse has the verdict of the other bindings of its
name in its scope, and none where it is the only one: nothing is resolved to
it on this platform.

A public member is reached in ways no reference shows. On a class a package
the repository ships offers, it is the package's interface and is not judged.
A package offers a class that is public with every class around it, at the top
of a module that ships as public (code_health.distribution) or whose shipping
does not resolve, where the module's __all__ lists it or the module declares
none; one such a module imports and exports, wherever it is declared; and with
either, every class it inherits a member from.
Elsewhere code holds the object as another type: a stand-in for the class, an
argument typed as a protocol, a framework that looks the name up. So a public
method, property, class variable or nested class is also used when a tracked
Python file reads an attribute of that name on anything, or spells the name in
a string outside a docstring. An assigned attribute keeps the resolver's
verdict alone: attribute names repeat across unrelated classes, so another
class's read says nothing about this one.

A finding names the declaration with the classes around it, in the file that
declares it. A member of an unused class has none of its own.

A read or an import of a name that pyright cannot place, a use that lands on a
Protocol member of the same name, a class pattern's keyword, a name
__match_args__ lists for a positional one, and a name handed as a literal to
getattr, hasattr, attrgetter or methodcaller, alone or as a part of a dotted
path, may each be a use of any declaration with that name, so each counts as
one, in a measured file or a tracked one outside the measurement. An attribute read that
lands on a member declared outside the repository may be reading a class of
the repository that stands in for the outside one, so it counts as a use of
every member with that name, and so does an attribute read in a tracked
template or a template held in a Python string (code_health.outside_python). A
call spelled getattr or hasattr whose name is
not a literal reads a name built at run time: each name a string of the same
top-level statement is, the list such a loop reads from, counts as read, and
where the name is formatted after a literal start and read on a method's own
receiver, so does each member of a related class whose name starts that way.
Any other name built at run time, such as HTTP-verb dispatch or generated code,
is not seen. A variable
or attribute that is only ever assigned is unused. An augmented assignment
that leaves a value of an immutable type replaces what it read and only
assigns; any other may change an object another name holds, so it reads.

A use is looked for where it is cheapest to find first: in the declaring file
and in the imports of the name, then at every other occurrence of the spelling
in the measured files, in their string literals, where an annotation may name
it, and last in the tracked Python files outside the measurement. There a use
is a reference pyright resolves to the declaration, in a file that spells the
name and, for a module-level name, imports relatively or names the declaring
module by a component of its path. Such a file often loads the module from a
path only known at run time, so a read or an import of a module-level name
that pyright cannot place counts there too when the file names the declaring
module itself. It often holds an object nothing types, a fixture or a helper's
result, so a read of a member's name that pyright cannot place counts there in
any file. The order changes how long the answer takes, never the answer.

Imports and local variables are left to Ruff's F401 and F841.
"""

from functools import cache
import ast
import posixpath
import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import PurePosixPath

from code_health.candidates import Candidate, Declared, defined_in
from code_health.hierarchy import standard_class
from code_health.imports import module_name
from code_health.occurrences import Occurrences, accessor
from code_health.outside_python import OutsidePython, unmeasured_parse
from code_health.resolver import Declaration, Resolver, Site, expression_site
from code_health.specimens import Specimens
from code_health.symbols import FUNCTIONS
from code_health.visibility import all_listing

INERT_DECORATORS = frozenset(
    {
        ("builtins", "property"),
        ("builtins", "staticmethod"),
        ("builtins", "classmethod"),
        ("functools", "wraps"),
        ("functools", "cache"),
        ("functools", "lru_cache"),
        ("functools", "cached_property"),
        ("functools", "total_ordering"),
        ("dataclasses", "dataclass"),
        ("contextlib", "contextmanager"),
        ("contextlib", "asynccontextmanager"),
        ("typing", "overload"),
        ("typing", "final"),
        ("typing", "override"),
        ("typing_extensions", "override"),
        ("abc", "abstractmethod"),
    }
)
PROPERTIES = frozenset({("builtins", "property"), ("functools", "cached_property")})
PROTOCOLS = frozenset({("typing", "Protocol"), ("typing_extensions", "Protocol")})
INERT_BASES = frozenset({("abc", "ABC"), ("typing", "Generic"), *PROTOCOLS})
INERT_METACLASSES = frozenset({("abc", "ABCMeta")})
NAME_READERS = {
    ("builtins", "getattr"): 1,
    ("builtins", "hasattr"): 1,
    ("operator", "attrgetter"): None,
    ("operator", "methodcaller"): 0,
}
ASSIGNED_KINDS = frozenset({"variable", "field", "attribute"})
DATACLASS = ("dataclasses", "dataclass")
IMMUTABLE = frozenset(
    {
        "int",
        "float",
        "complex",
        "bool",
        "str",
        "bytes",
        "tuple",
        "frozenset",
        "Literal",
        "LiteralString",
        "None",
    }
)
RELATIVE_IMPORT = r"\bfrom\s+\."
SPHINX_CONFIGURATION = "conf.py"
SPHINX_ROOTS = ("index.rst", "index.md", "index.txt")


def inert_base(declared: Declaration) -> bool:
    if (declared.module, declared.name) in INERT_BASES:
        return True
    standard = standard_class(f"{declared.module}.{declared.name}")
    return standard is object or (
        standard is not None and issubclass(standard, BaseException)
    )


def finds_subclasses(declared: Declaration) -> bool:
    """A base outside the repository that may find a subclass no code refers
    to: any outside the standard library, and unittest's TestCase."""
    if declared.module.split(".")[0] not in sys.stdlib_module_names:
        return True
    standard = standard_class(f"{declared.module}.{declared.name}")
    return standard is not None and issubclass(standard, unittest.TestCase)


def path_names(path: str) -> list[str]:
    """The names an import of the file may spell: its folders and its stem."""
    located = PurePosixPath(path)
    return [*located.parts[:-1], located.stem]


def union_members(printed: str) -> list[str]:
    """The members of a type as pyright prints it, split at the bars outside
    its brackets and its quoted literals."""
    members, depth, start, quote, escaped = [], 0, 0, "", False
    for index, character in enumerate(printed):
        if quote:
            quote = "" if character == quote and not escaped else quote
            escaped = character == "\\" and not escaped
            continue
        quote = character if character in "'\"" else ""
        depth += (character == "[") - (character == "]")
        if character == "|" and not depth:
            members.append(printed[start:index].strip())
            start = index + 1
    return [*members, printed[start:].strip()]


def spelling(names) -> str:
    """A pattern for a text that spells one of the names as a word. An empty
    name is the module of an initializer in the repository's root, imported
    under whatever name its checkout has, and any text may spell that."""
    return "|".join(rf"\b{re.escape(name)}\b" for name in names)


@dataclass(frozen=True)
class Sought:
    """The declarations one verdict is about: their sites, the module and name
    of each one outside the repository that an import beside them binds to the
    same name, whether only a read uses them, and whether they are members of
    a class."""

    declared: set[Site]
    outside: set[tuple[str, str]]
    assigned_only: bool
    member: bool


def lands(declaration: Declaration, sought: Sought) -> bool:
    """Whether a reference that reaches the declaration uses what is sought:
    it is one of them, or a stub's for the module that declares one."""
    site = declaration.site
    if site is None or site in sought.declared:
        return site is not None
    described = site.path.removesuffix("i") if site.path.endswith(".pyi") else None
    return any(found.path == described for found in sought.declared)


class UnusedCode:
    def __init__(
        self,
        resolver: Resolver,
        trees: dict[str, ast.Module],
        tracked: set[str],
        specimens: Specimens,
    ):
        """trees holds the measured modules, tracked every path the repository
        lists."""
        self.resolver = resolver
        self.specimens = specimens
        self.collection = specimens.runner
        self.shipped = specimens.shipped
        self.tops = {id(tree): path for path, tree in trees.items()}
        self.unmeasured = tracked - set(trees)
        self.dotted = {path: module_name(path, set(trees)) for path in trees}
        self.outside = OutsidePython(resolver.root, self.unmeasured, specimens)
        self.read: dict[str, Occurrences | None] = {}
        modules = [
            Declared(path, tree, resolver)
            for path, tree in trees.items()
            if not path.endswith(".pyi")
        ]
        self.candidates = [found for module in modules for found in module.found]
        self.imported = {
            key: sites for module in modules for key, sites in module.imports.items()
        }
        self.declaring = {c.site: c for c in self.candidates}
        self.classes = {c.site: c for c in self.candidates if c.kind == "class"}
        self.by_node = {id(c.node): c for c in self.classes.values()}
        self.owners = {c.site: c.owner for c in self.candidates}
        self.receivers = {site for module in modules for site in module.receivers}
        self.aliases = {
            site: named for module in modules for site, named in module.aliases.items()
        }
        self.members: dict[tuple[int, str], list[Site]] = {}
        self.bound: dict[tuple[int, str], list[Site]] = {}
        for candidate in self.candidates:
            for holder, held in (
                (candidate.owner, self.members),
                (candidate.scope, self.bound),
            ):
                if holder is not None:
                    key = (id(holder), candidate.name)
                    held.setdefault(key, []).append(candidate.site)
        for module in modules:
            for site, (owner, name) in module.slots.items():
                self.owners[site] = owner
                self.members.setdefault((id(owner), name), []).append(site)
        self.occurrences = Occurrences(trees)
        self.listings = {path: all_listing(tree) for path, tree in trees.items()}
        self.above: dict[int, set[int]] = {}
        self.below: dict[int, set[int]] = {}
        self.related: dict[int, set[int]] = {}
        self.exposed: set[int] = set()
        self.found: set[int] = set()
        self.finders: set[int] | None = None
        self.exports: set[Site] | None = None
        self.verdicts: dict[Site, bool] = {}

    def spelled(self, path: str, expression: ast.expr) -> list[Declaration]:
        site = expression_site(path, expression)
        return self.resolver.declarations(site) if site else []

    def handed_on(self, path: str, node) -> bool:
        """A decorator that may keep the definition: any but the inert ones
        and a property's own accessors."""
        return any(
            not declared
            or any((d.module, d.name) not in INERT_DECORATORS for d in declared)
            for declared in (
                self.spelled(path, decorator)
                for decorator in node.decorator_list
                if not accessor(decorator, node.name)
            )
        )

    def built_outside(
        self, path: str, owner: ast.ClassDef, bases: list[Declaration]
    ) -> bool:
        """A class statement that hands the class to code that may keep it: a
        decorator that is not inert, a metaclass that is not inert, or another
        keyword, which a base receives in __init_subclass__, unless every base
        is the standard library's, whose own keep no class."""
        standard = all(
            base.site is None and base.module.split(".")[0] in sys.stdlib_module_names
            for base in bases
        )
        for keyword in owner.keywords:
            if keyword.arg != "metaclass":
                if not standard:
                    return True
                continue
            declared = self.spelled(path, keyword.value)
            if not declared or any(
                (d.module, d.name) not in INERT_METACLASSES for d in declared
            ):
                return True
        return self.handed_on(path, owner)

    def base(
        self, path: str, expression: ast.expr, followed: frozenset[Site] = frozenset()
    ) -> list[Declaration]:
        """The declarations a base names, through each variable of the
        repository that only names another."""
        named = []
        for declaration in self.spelled(path, expression):
            aliased = self.aliases.get(declaration.site)
            if aliased is None or declaration.site in followed:
                named.append(declaration)
            else:
                through = followed | {declaration.site}
                named += self.base(declaration.site.path, aliased, through)
        return named

    def parents(self) -> dict[int, set[int]]:
        """Each class's bases in the repository; into exposed, each class whose
        members code outside the repository may call; into found, each class
        such code may find with nothing referring to it."""
        direct: dict[int, set[int]] = {}
        for site, declared in self.classes.items():
            node = declared.node
            bases = [self.base(site.path, base) for base in node.bases]
            named = [declaration for found in bases for declaration in found]
            outside = [d for d in named if d.site not in self.classes]
            direct[id(node)] = {
                id(self.classes[d.site].node) for d in named if d.site in self.classes
            }
            built = not all(bases) or self.built_outside(site.path, node, named)
            if built or any(d.site is not None or not inert_base(d) for d in outside):
                self.exposed.add(id(node))
            if built or any(d.site is not None or finds_subclasses(d) for d in outside):
                self.found.add(id(node))
        return direct

    def related_classes(self) -> dict[int, set[int]]:
        """For each class of the repository, the classes a call on it may
        reach a member of: itself, its descendants and every ancestor of any of
        them, so two bases of one class are related. Into above, itself with
        its ancestors; into below, itself with its descendants."""
        if self.related:
            return self.related
        parents = self.parents()
        for start in parents:
            above, pending = {start}, [start]
            while pending:
                for parent in parents[pending.pop()] - above:
                    above.add(parent)
                    pending.append(parent)
            self.above[start] = above
        for start, above in self.above.items():
            for ancestor in above:
                self.below.setdefault(ancestor, set()).add(start)
        for start, below in self.below.items():
            self.related[start] = set().union(*(self.above[lower] for lower in below))
        return self.related

    def subclass_finders(self) -> set[int]:
        """The classes whose subclasses the repository's code finds with
        nothing referring to them: one that defines __init_subclass__, one
        whose __subclasses__ is read, by name or on the receiver of a method
        in its body, and every class when a read is on neither."""
        if self.finders is None:
            self.finders = {
                identity
                for identity, declared in self.by_node.items()
                if any(
                    member.name == "__init_subclass__"
                    for member in defined_in(declared.node)
                )
            }
            for path, read in self.occurrences.subclass_reads:
                named = self.spelled(path, read.value)
                own = any(found.site in self.receivers for found in named)
                asked = [
                    id(self.classes[found.site].node)
                    for found in named
                    if found.site in self.classes
                ]
                around = [
                    identity
                    for identity, declared in self.by_node.items()
                    if declared.site.path == path
                    and declared.node.lineno <= read.lineno <= declared.node.end_lineno
                ]
                self.finders.update(asked or (around if own else self.by_node))
        return self.finders

    def protocol(self, path: str, owner: ast.ClassDef) -> bool:
        return any(
            (declaration.module, declaration.name) in PROTOCOLS
            for base in owner.bases
            for declaration in self.spelled(path, base)
        )

    def collectable(self, candidate: Candidate) -> bool:
        """Whether a test runner may read the name, where it is bound or in a
        class of the repository that inherits it, or may collect from the
        class a test it holds or inherits."""
        path, node = candidate.site.path, candidate.node
        if isinstance(node, ast.ClassDef):
            self.related_classes()
            return any(
                self.collection.may_collect(path, node, member.name)
                for ancestor in self.above[id(node)]
                for member in defined_in(self.by_node[ancestor].node)
            )
        if candidate.scope is None:
            return False
        holders = [(path, candidate.scope)]
        if candidate.owner is not None:
            self.related_classes()
            inheriting = (
                self.by_node[lower] for lower in self.below[id(candidate.owner)]
            )
            holders = [(lower.site.path, lower.node) for lower in inheriting]
        return any(
            self.collection.may_collect(held, holder, candidate.name)
            for held, holder in holders
        )

    def loaded_by_name(self, candidate: Candidate) -> bool:
        """Whether a tool reads the name from outside Python: the path an
        entry point of the project loads or a dotted or colon reference resolves
        to passes through it, a type checker alone reads its module, or it is a
        module-level name of a Sphinx configuration."""
        path = candidate.site.path
        if candidate.scope is None:
            return False
        if self.specimens.typing_fixture(path):
            return True
        holders = reversed(self.around(candidate.owner))
        loaded = ".".join([*(holder.name for holder in holders), candidate.name])
        if self.outside.uses_target(path, loaded) or any(
            module == self.dotted[path] and f"{target}.".startswith(f"{loaded}.")
            for module, target in self.collection.projects.entry_points
        ):
            return True
        folder, base = posixpath.split(path)
        beside = {posixpath.join(folder, root) for root in SPHINX_ROOTS}
        configuration = base == SPHINX_CONFIGURATION and bool(beside & self.unmeasured)
        return configuration and isinstance(candidate.scope, ast.Module)

    def exempt(self, candidate: Candidate) -> bool:
        path, name, node = candidate.site.path, candidate.name, candidate.node
        if name.startswith("_") and name.endswith("_"):
            return True
        listed, undecided = self.listings[path]
        top = isinstance(candidate.scope, ast.Module)
        if top and (undecided or name in (listed or ())):
            return True
        if self.loaded_by_name(candidate):
            return True
        if candidate.owner is not None and self.protocol(path, candidate.owner):
            return True
        if self.collectable(candidate):
            return True
        if isinstance(node, ast.ClassDef):
            self.related_classes()
            above = self.above[id(node)]
            finders = (above - {id(node)}) & self.subclass_finders()
            return bool(above & self.found or finders)
        if candidate.kind == "field":
            decorators = candidate.owner.decorator_list
            return any(
                (declared.module, declared.name) == DATACLASS
                for decorator in decorators
                for declared in self.spelled(path, decorator)
            )
        return isinstance(node, FUNCTIONS) and self.handed_on(path, node)

    def declaration(self, candidate: Candidate) -> Site | None:
        """The site whose references answer for the candidate; None for an
        attribute of an object declared outside the repository or in a stub,
        or one pyright cannot place."""
        if candidate.kind != "attribute":
            return candidate.site
        declared = self.resolver.declarations(candidate.site)
        if not declared or any(
            found.site is None or found.site.path.endswith(".pyi") for found in declared
        ):
            return None
        return declared[0].site

    def uses(self, site: Site, sought: "Sought") -> bool:
        """Whether the occurrence at the site uses what is sought: it refers to
        one of the declarations, lands on a Protocol member, reads a member of
        that name declared outside, or is a read pyright cannot place."""
        if sought.assigned_only and self.assigns(self.occurrences, site):
            return False
        found = self.resolver.declarations(site)
        if not found:
            return site in self.occurrences.reads
        for declaration in found:
            if lands(declaration, sought):
                return True
            if declaration.site is None:
                if (declaration.module, declaration.name) in sought.outside:
                    return True
                if sought.member and site in self.occurrences.attributes:
                    return True
                continue
            if self.stated(declaration):
                return True
        return False

    def stated(self, declaration: Declaration) -> bool:
        """Whether the declaration is a member a Protocol of the repository
        declares."""
        owner = self.owners.get(declaration.site)
        return owner is not None and self.protocol(declaration.site.path, owner)

    def assigns(self, occurrences: Occurrences, site: Site) -> bool:
        """Whether the place only assigns: an assignment or deletion, or an
        augmented assignment that leaves a value of an immutable type."""
        if site not in occurrences.augmented:
            return site in occurrences.writes
        left = union_members(self.resolver.printed_type(site))
        return all(member.split("[")[0] in IMMUTABLE for member in left)

    def read_at(self, places: list[Site], sought: Sought) -> bool:
        """Whether a place other than the declarations uses what is sought; an
        augmented assignment may be both a declaration and a read."""
        return any(
            self.uses(found, sought)
            for found in places
            if found not in sought.declared or found in self.occurrences.augmented
        )

    def read_by_name(self, occurrences: Occurrences, name: str) -> bool:
        """A class pattern's keyword, a name a string holds beside a read of a
        computed name, or a literal handed to a function that reads the
        attribute it names, which attrgetter does in any argument."""
        if name in occurrences.patterned or name in occurrences.computed:
            return True
        return any(
            reader in NAME_READERS and NAME_READERS[reader] in (index, None)
            for path, call, index in occurrences.literals.get(name, [])
            for reader in ((d.module, d.name) for d in self.spelled(path, call.func))
        )

    def quoted(self, occurrences: Occurrences, name: str) -> list[Site]:
        """Each place a string literal spells the name as a word."""
        word = re.compile(rb"\b" + re.escape(name.encode()) + rb"\b")
        sites = []
        for path, node in occurrences.strings:
            if name not in node.value:
                continue
            for number in range(node.lineno, node.end_lineno + 1):
                text = self.resolver.lines[path][number - 1].encode()
                start = node.col_offset if number == node.lineno else 0
                end = node.end_col_offset if number == node.end_lineno else len(text)
                sites += [
                    Site(path, number, start + spelled.start())
                    for spelled in word.finditer(text[start:end])
                ]
        return sites

    def occurrences_in(self, path: str, text: str) -> Occurrences | None:
        """What a file outside the measurement spells, once the server has it;
        None for a file Python cannot parse."""
        if path not in self.read:
            try:
                tree = unmeasured_parse(text, path)
            except (SyntaxError, ValueError):
                self.read[path] = None
            else:
                self.resolver.open(path, text, tree)
                self.read[path] = Occurrences({path: tree})
        return self.read[path]

    def used_elsewhere(self, site: Site, name: str, sought: Sought) -> bool:
        """Whether a tracked Python file outside the measurement refers to one
        of the declarations."""
        dotted = self.dotted[site.path].split(".")
        anchors = {*path_names(site.path), *dotted}
        named = re.compile(f"{RELATIVE_IMPORT}|{spelling(anchors)}")
        word = re.compile(spelling([name]))
        module = re.compile(spelling(dotted[-1:]))
        for path, text in self.outside.elsewhere.items():
            if not word.search(text) or not (sought.member or named.search(text)):
                continue
            spelled = self.occurrences_in(path, text)
            if spelled is None:
                continue
            if self.read_by_name(spelled, name):
                return True
            loaded = sought.member or module.search(text) is not None
            for found in [*spelled.spelled.get(name, []), *self.quoted(spelled, name)]:
                if sought.assigned_only and self.assigns(spelled, found):
                    continue
                declared = self.resolver.declarations(found)
                if any(
                    lands(declaration, sought)
                    or (declaration.module, declaration.name) in sought.outside
                    or (sought.member and self.stated(declaration))
                    for declaration in declared
                ):
                    return True
                if loaded and not declared and found in spelled.reads:
                    return True
        return False

    def family(self, site: Site, candidate: Candidate) -> list[Site]:
        """The declarations that are one with the candidate's: itself, each
        binding of its name in its scope and, for a member, each one a related
        class declares."""
        owner, name = self.owners.get(site), candidate.name
        symbols = [site, *self.bound.get((id(candidate.scope), name), [])]
        if owner is not None:
            symbols += [
                member
                for relative in self.related_classes()[id(owner)]
                for member in self.members.get((relative, name), [])
            ]
        return symbols

    def around(self, owner: ast.ClassDef | None) -> list[Candidate]:
        """The class and each class it is nested in, innermost first."""
        holders = []
        while owner is not None:
            holders.append(self.by_node[id(owner)])
            owner = holders[-1].owner
        return holders

    def exported(self) -> set[Site]:
        """The classes a module that ships as public, or whose shipping does
        not resolve, imports and exports: the ones its __all__ lists, or every
        public one where a package initializer declares none."""
        if self.exports is None:
            self.exports = set()
            for (scope, name), sites in self.imported.items():
                path = self.tops.get(scope)
                if path is None or self.shipped.status(path) == "unshipped":
                    continue
                listed, undecided = self.listings[path]
                initializer = posixpath.basename(path) == "__init__.py"
                public = initializer and not name.startswith("_")
                if undecided or (public if listed is None else name in listed):
                    self.exports.update(
                        found.site
                        for site in sites
                        for found in self.resolver.declarations(site)
                        if found.site in self.classes
                    )
        return self.exports

    def offered(self, declared: Candidate) -> bool:
        """Whether a package the repository ships offers the class: it and
        each class around it are public, and the outermost is exported by a
        shipped module, or sits at the top of one that ships as public or
        whose shipping does not resolve, listed by its __all__ where it
        declares one."""
        holders = [declared, *self.around(declared.owner)]
        outer = holders[-1]
        hidden = any(holder.name.startswith("_") for holder in holders)
        if hidden or not isinstance(outer.scope, ast.Module):
            return False
        path = outer.site.path
        if outer.site in self.exported():
            return True
        if self.shipped.status(path) == "unshipped":
            return False
        listed, undecided = self.listings[path]
        return undecided or listed is None or outer.name in listed

    def interface(self, site: Site, name: str) -> bool:
        """Whether the declaration is a public member of a class a shipped
        package offers, its own or one that inherits it."""
        owner = self.owners.get(site)
        if owner is None or name.startswith("_"):
            return False
        self.related_classes()
        return any(self.offered(self.by_node[lower]) for lower in self.below[id(owner)])

    def dispatched(self, owner: ast.ClassDef, name: str) -> bool:
        """Whether a method of a related class reads, on its own receiver, a
        name formatted after a literal start the member's name has."""
        related = [self.by_node[found] for found in self.related_classes()[id(owner)]]
        return any(
            name.startswith(start)
            and any(
                found.site in self.receivers
                for found in self.spelled(path, call.args[0])
            )
            and any(
                holder.site.path == path
                and holder.node.lineno <= call.lineno <= holder.node.end_lineno
                for holder in related
            )
            for path, call, start in self.occurrences.prefixed
        )

    def named(self, name: str) -> bool:
        """Whether a tracked Python file reads an attribute of that name on
        anything, or spells the name in a string outside a docstring."""
        if name in self.occurrences.named:
            return True
        word = re.compile(spelling([name]))
        for path, text in self.outside.elsewhere.items():
            spelled = self.occurrences_in(path, text) if word.search(text) else None
            if spelled is not None and name in spelled.named:
                return True
        return False

    @cache
    def declared(
        self, symbols: frozenset[Site], twins: frozenset[Site]
    ) -> tuple[set[Site], set[tuple[str, str]]]:
        """Every site the symbols and their imported twins resolve to, and the
        outside names the twins resolve to. Rebindings of one name share one
        family, so each asks for the same answer."""
        declared, outside = {*symbols, *twins}, set()
        for symbol in [*symbols, *twins]:
            for found in self.resolver.declarations(symbol):
                if found.site is not None:
                    declared.add(found.site)
                elif symbol in twins:
                    outside.add((found.module, found.name))
        return declared, outside

    def unread(self, site: Site, candidate: Candidate, symbols: list[Site]) -> bool:
        owner, name = self.owners.get(site), candidate.name
        related = self.related_classes()
        if any(
            related[id(holder.node)] & self.exposed for holder in self.around(owner)
        ):
            return False
        twins = self.imported.get((id(candidate.scope), name), [])
        declared, outside = self.declared(frozenset(symbols), frozenset(twins))
        member = owner is not None or candidate.kind == "attribute"
        assigned_only = candidate.kind in ASSIGNED_KINDS
        sought = Sought(declared, outside, assigned_only, member)
        spelled = self.occurrences.spelled.get(name, [])
        near = [found for found in spelled if found.path == site.path]
        near += self.occurrences.imports.get(name, [])
        far = [found for found in spelled if found.path != site.path]
        if (
            (member and name in self.outside.template_reads)
            or self.read_at(near, sought)
            or self.read_at(far, sought)
            or self.read_at(self.quoted(self.occurrences, name), sought)
        ):
            return False
        dispatched = owner is not None and self.dispatched(owner, name)
        if dispatched or self.read_by_name(self.occurrences, name):
            return False
        if self.used_elsewhere(site, name, sought):
            return False
        held = owner is not None and candidate.kind != "attribute"
        return not (held and not name.startswith("_") and self.named(name))

    def unused(self, candidate: Candidate) -> bool:
        """The verdict on the candidate's declaration, reached from whichever
        candidate asks first: unused when nothing makes it or a declaration
        that is one with it used without a reference, and nothing reads it. A
        member of a shipped package's interface, a declaration in code pyright
        does not analyse that is its scope's only binding of the name, and a
        member of a class that is itself unused, which that class's finding
        covers, has none."""
        site = self.declaration(candidate)
        if site is None:
            return False
        if site not in self.verdicts:
            declaring = self.declaring.get(site, candidate)
            symbols = self.family(site, declaring)
            twins = [self.declaring[twin] for twin in symbols if twin in self.declaring]
            kept = any(self.exempt(twin) for twin in [declaring, *twins])
            kept = kept or any(self.interface(twin, declaring.name) for twin in symbols)
            self.verdicts[site] = not kept and self.unread(site, declaring, symbols)
        if not self.verdicts[site]:
            return False
        bound = (id(candidate.scope), candidate.name)
        twins = len(self.bound.get(bound, [])) > 1 or bound in self.imported
        if not twins and self.resolver.unanalysed(candidate.site.path, candidate.node):
            return False
        holders = self.around(self.owners.get(site))
        return not any(self.unused(holder) for holder in holders[:1])

    def kind(self, candidate: Candidate) -> str:
        if candidate.kind == "field":
            return "variable"
        if candidate.kind != "method":
            return candidate.kind
        decorators = candidate.node.decorator_list
        decorated = {
            (declaration.module, declaration.name)
            for decorator in decorators
            for declaration in self.spelled(candidate.site.path, decorator)
        }
        completes = any(accessor(decorator, candidate.name) for decorator in decorators)
        return "property" if completes or PROPERTIES & decorated else "method"

    def findings(self) -> dict[str, list[str]]:
        found = set()
        for candidate in self.candidates:
            site = self.declaration(candidate) if self.unused(candidate) else None
            if site is None:
                continue
            declaring = self.declaring.get(site, candidate)
            holders = reversed(self.around(self.owners.get(site)))
            named = ".".join([*(holder.name for holder in holders), declaring.name])
            found.add((site.path, f"unused {self.kind(declaring)} '{named}'"))
        by_file: dict[str, list[str]] = {}
        for path, finding in sorted(found):
            by_file.setdefault(path, []).append(finding)
        return by_file
