"""Single-call hops, decided by deleting the function and naming its owner at
every caller.

A candidate is a function whose body, after its docstring, is one expression
that calls something and has no logic of its own: no branch, comparison,
arithmetic, comprehension, lambda, assignment or yield. Every argument it
passes is a permitted argument: a name or a dotted name, a literal or a
display of literals, a spread of one, or a composed call whose own arguments
are permitted. Anything else, such as an item (argument[0]), a format string
(f"{argument}") or a display built around a value, transforms the argument and
is not forwarding. One call and composed calls are both candidates, and a
format string or %-template at the root binds a literal around its calls.

For each candidate the engine finds every reference to it in the repository
and binds each call site to the parameters, then classifies what deleting the
function would cost: whether every caller could name the owner instead. It
reports the decision with its call-site evidence and rewrites nothing. The
cost decides, first row first:

- contract: the function itself is needed. A decorator other than the builtin
  descriptors receives it; it is one of the data model's documented special
  methods; a decided base class or standard library base declares it, a
  decided subclass overrides it or a Protocol lists it; __all__ or a package
  initializer exports it, or exports its class or a subclass that inherits
  it; it is a public name of a public
  module of a package the repository distributes; a resolved reference does
  not call it, so it is passed, assigned or returned by identity; a call keeps
  the coroutine of an async def without awaiting it; a dotted string, a
  builtin getattr-family lookup, unittest.mock patch.object or patch.multiple,
  or pytest's monkeypatch.setattr or delattr names it, so a consumer needs its
  name; or a base class's own method calls it on its instance.
- not forwarding: a call site passes an argument that is not a name or a
  literal to a parameter the expression reads more than once, so the function
  holds that one evaluation.
- unresolved, reference: another definition binds the name in the same scope.
- unresolved, fact: with no resolved call, an export, lineage or string fact
  about it is undecided; such a candidate is incomplete, never unreferenced.
- unreferenced: nothing in the repository references it.
- recursion: a call the expression makes resolves to the function a caller
  would name it from.
- shared formula: two or more resolved calls reach it and it holds something
  deleting it would copy to each of them: its signature binds a default, it
  is async and awaits nothing, or its expression is more than one call, to a
  dotted name, that hands on the function's own parameters, each once, and
  returns what it gets. An argument that is a literal, a composed call, a
  field of a parameter or a name bound outside the function is held, and so
  is a part taken of the result; the function is the one place that formula
  is written.
- unresolved, receiver: a reference's receiver does not resolve, so a caller
  the engine cannot see may need the function.
- unresolved, proof: its package's build configuration does not resolve, its
  module's __all__ is undecided, a string may name it without resolving, a call
  site cannot be bound to its parameters, or a base in its lineage is not
  decided.
- hop: every resolved reference is a call. Reported with its call sites; the
  engine claims no inline that is equivalent at them.
- restated: a module-level hop or shared formula holds something and a
  referenced, undecorated module-level function of another file holds the
  same: the same statement and signature, with the parameters in the same
  places and of the same kinds, the same defaults, and every other name a
  builtin or bound by an import that resolves to the same file or, outside
  the repository, is spelled the same. A name its own module defines or a
  star import may bind makes a formula that module's alone, and a method's
  is its class's. The formula has several owners; each is reported with the
  others, and one definition should remain. A file that is a copy of
  another, statement for statement, is that file again and not a second
  owner. It says nothing about the callers, so an unresolved receiver or an
  unproven fact does not withhold it.

A fact decided from a name (an __all__ listing, a class base, the callee of a
getattr-family lookup or a patch, a builtin) comes from a single binding that
reaches the use unconditionally (code_health.reaching); anything else leaves
the fact undecided, and an undecided fact is incomplete analysis, reported
with its cause at its site.

Python's own visibility decides a public name: when a module declares __all__,
that list is authoritative; without one, a name is public when it has no
leading underscore. A method is public when its class is public by that rule
and its own name has no leading underscore. An undecided listing, and every
name a re-export or star import takes from one, is incomplete analysis at the
defining module.
"""

import ast
import builtins
import copy
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from code_health.forwarding import formals, forwarded, held, literal
from code_health.hierarchy import DATA_MODEL
from code_health.repository import Repository, root_name, split_reference
from code_health.symbols import (
    FUNCTIONS,
    Evaluation,
    binds,
    enclosing_class,
    local_nodes,
)
from code_health.visibility import assigned

DESCRIPTORS = {"property": property, "staticmethod": staticmethod}
DESCRIPTORS["classmethod"] = classmethod


@dataclass
class Inline:
    """The deletion of one candidate: its references and what inlining costs."""

    calls: list[tuple[str, ast.AST]] = field(default_factory=list)
    identities: list[str] = field(default_factory=list)
    named: list[str] = field(default_factory=list)
    unnamed: list[str] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    recursive: list[str] = field(default_factory=list)
    unawaited: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    repeated: list[str] = field(default_factory=list)

    def merge(self, site: "Inline") -> None:
        """Add one call site's inline to the deletion."""
        for name, recorded in vars(site).items():
            getattr(self, name).extend(recorded)


@dataclass
class Decision:
    path: str
    line: int
    qualname: str
    expression: str
    row: str
    cause: str
    reason: str
    inline: Inline
    formula: str | None


def builtin_descriptor(decorator: ast.expr, bound: set[str]):
    if isinstance(decorator, ast.Name) and decorator.id not in bound:
        return DESCRIPTORS.get(decorator.id)
    return None


def shadowed(scopes: list[ast.AST], ref: ast.Name, outer: ast.AST) -> bool:
    """A function between the reference and the definition's scope binds the
    name itself, so the reference is to that local."""
    for scope in scopes:
        if scope is outer or isinstance(scope, ast.Module):
            return False
        if isinstance(scope, (*FUNCTIONS, ast.Lambda)) and binds(scope, ref.id):
            return True
    return False


def inside(node: ast.AST, container: ast.AST, parent_of: dict) -> bool:
    current = node
    while current is not None:
        if current is container:
            return True
        current = parent_of.get(id(current))
    return False


class Deletion:
    """Delete one candidate and inline its expression at every reference."""

    def __init__(self, repository: Repository, path: str, node, expression):
        self.repository = repository
        self.path = path
        self.node = node
        self.expression = expression
        self.owner = repository.symbols.scopes(path, node)[0]
        bound = repository.bindings[path]
        self.descriptors = [
            builtin_descriptor(decorator, bound) for decorator in node.decorator_list
        ]

    def references(self) -> list[tuple[str, ast.AST]]:
        repository, name = self.repository, self.node.name
        parent_of = repository.parent_of
        scopes = repository.symbols.scopes
        if isinstance(self.owner, ast.ClassDef):
            found = [
                (self.path, ref)
                for ref in repository.names[self.path].get(name, [])
                if scopes(self.path, ref)[0] is self.owner
            ]
            found += repository.attributes.get(name, [])
        elif isinstance(self.owner, ast.Module):
            found = [(self.path, ref) for ref in repository.names[self.path][name]]
            for path, alias in repository.aliases.get((self.path, name), []):
                found += [(path, ref) for ref in repository.names[path].get(alias, [])]
            found = [
                (path, ref)
                for path, ref in found
                if not shadowed(scopes(path, ref), ref, repository.trees[path])
            ]
            found += [
                (path, ref)
                for path, ref in repository.attributes.get(name, [])
                if (root := root_name(ref)) in repository.module_files[path]
                and root in repository.module_aliases(path, self.path)
            ]
        else:
            found = [
                (self.path, ref)
                for ref in repository.names[self.path].get(name, [])
                if inside(ref, self.owner, parent_of[self.path])
                and not shadowed(scopes(self.path, ref), ref, self.owner)
            ]
        return [
            (path, ref)
            for path, ref in found
            if not inside(ref, self.node, parent_of[path])
        ]

    def alternative(self) -> str:
        """Where another definition binds the same name in the same scope, so a
        reference may reach either body."""
        for other in local_nodes(self.owner):
            names = [t.id for t in assigned(other) if isinstance(t, ast.Name)]
            if isinstance(other, FUNCTIONS) and other is not self.node:
                names.append(other.name)
            if self.node.name in names:
                return f"line {other.lineno} binds the name again"
        return ""

    def contract(self) -> str:
        name = self.node.name
        if any(descriptor is None for descriptor in self.descriptors):
            return "a decorator receives it"
        special = name[2:-2] if name.startswith("__") and name.endswith("__") else ""
        if special in DATA_MODEL and isinstance(self.owner, ast.ClassDef):
            return "the data model calls it"
        if isinstance(self.owner, ast.ClassDef):
            why = self.repository.hierarchy.contract(self.owner, name)
            if why:
                return why
            files = self.repository.symbols.files
            exported = [
                cls
                for cls in (
                    self.owner,
                    *self.repository.hierarchy.heirs[id(self.owner)],
                )
                if (files[id(cls)], cls.name) in self.repository.exports
            ]
            if exported and not name.startswith("_"):
                return f"exported class {exported[0].name} carries it"
        if isinstance(self.owner, ast.Module) and (
            (self.path, name) in self.repository.exports
        ):
            return "the package exports it"
        if self.published() == "public":
            return "a public name of a public module of a distributed package"
        return ""

    def exported_name(self) -> str | None:
        """The module-scope name Python's visibility decides for this
        definition: the function's, or its class's; None for a nested or
        underscore method, or a class not at module scope."""
        if isinstance(self.owner, ast.Module):
            return self.node.name
        if not isinstance(self.owner, ast.ClassDef) or self.node.name.startswith("_"):
            return None
        outer = self.repository.symbols.scopes(self.path, self.owner)[0]
        return self.owner.name if isinstance(outer, ast.Module) else None

    def published(self) -> str:
        """ "public", "unshipped", "unresolved" (the build configuration does not
        resolve) or "dynamic" (__all__ is not literal and does not list it)."""
        name = self.exported_name()
        if name is None:
            return "unshipped"
        status = self.repository.shipped.status(self.path)
        if status != "public":
            return status
        listed, dynamic = self.repository.listings[self.path]
        if dynamic:
            return "dynamic"
        if listed is not None:
            return "public" if name in listed else "unshipped"
        return "unshipped" if name.startswith("_") else "public"

    def reaches(self, caller: Evaluation, receiver: ast.expr) -> str:
        """Whether a method looked up on a receiver reaches this definition:
        "owner" when the receiver resolves to this class or a class that
        inherits it; "hook" when a base class's own method looks it up on its
        instance; "elsewhere" when the receiver resolves only to other classes,
        modules, functions or values from outside the repository; "unresolved"
        when the receiver does not resolve."""
        resolution = caller.resolve(receiver)
        if resolution is None:
            return "unresolved"
        ancestors = self.repository.symbols.ancestors
        calling_class = enclosing_class(caller.scopes)
        if (
            isinstance(receiver, ast.Name)
            and any(a is calling_class for a in ancestors(self.owner))
            and any(
                kind == "instance" and node is calling_class
                for kind, node in resolution
            )
        ):
            return "hook"
        if any(
            kind in ("class", "instance")
            and (node is self.owner or any(a is self.owner for a in ancestors(node)))
            for kind, node in resolution
        ):
            return "owner"
        return "elsewhere"

    def target(self, caller: Evaluation, ref: ast.AST) -> str:
        """Whether a reference reaches this definition. A name already resolved
        by scope reaches it; a method reference by its receiver; a module
        attribute when the attribute resolves to this function."""
        if isinstance(ref, ast.Name):
            return "owner"
        if isinstance(self.owner, ast.ClassDef):
            return self.reaches(caller, ref.value)
        resolution = caller.resolve(ref)
        if resolution is None:
            return "unresolved"
        return "owner" if ("function", self.node) in resolution else "elsewhere"

    def named_by(self, path: str, node: ast.AST, receiver) -> str:
        """Whether a name consumer reaches this definition: a dotted string by
        the module and attributes it names, a consumer of an object by that
        object, decided by a single reaching binding, and a consumer of a
        module named by a string by that module's attribute."""
        repository = self.repository
        if receiver is None:
            resolution = repository.dotted(split_reference(node.value))
        elif isinstance(receiver, ast.Constant) and isinstance(receiver.value, str):
            parts = split_reference(receiver.value)
            resolution = (
                None if parts is None else repository.dotted([*parts, self.node.name])
            )
        else:
            caller = Evaluation(repository.symbols, path, node, {})
            if not repository.hierarchy.reaching.certain(caller, receiver):
                return "unresolved"
            if isinstance(self.owner, ast.ClassDef):
                return self.reaches(caller, receiver)
            resolution = caller.resolve(receiver)
            if resolution is not None:
                resolution = repository.symbols.member(resolution, self.node.name)
        if resolution is None:
            return "unresolved"
        return "owner" if ("function", self.node) in resolution else "elsewhere"

    def strings_naming(self, deletion: Inline) -> None:
        """Record the name consumers that resolve to this definition, and those
        that may name it without resolving."""
        repository = self.repository
        consumers = []
        for path, constant in repository.strings.get(self.node.name, []):
            consumer = repository.consumers.consumer(path, constant)
            consumers.append((path, constant, consumer))
        for path, keyword, call in repository.keyword_names.get(self.node.name, []):
            consumers.append((path, keyword, repository.consumers.multiple(path, call)))
        for path, node, consumer in consumers:
            if inside(node, self.node, repository.parent_of[path]):
                continue
            site = f"{path}:{node.lineno}"
            if consumer is not None and consumer[0] == "unresolved":
                deletion.unnamed.append(site)
                continue
            reach = self.named_by(path, node, None if consumer is None else consumer[1])
            if reach == "owner":
                deletion.named.append(site)
            elif reach == "unresolved":
                deletion.unnamed.append(site)

    def inline(self) -> Inline:
        node, repository = self.node, self.repository
        deletion = Inline()
        self.strings_naming(deletion)
        accessed = property in self.descriptors
        coroutine = isinstance(node, ast.AsyncFunctionDef)
        noncalls = {"hook": deletion.hooks, "unresolved": deletion.unresolved}
        for path, ref in self.references():
            parent_of = repository.parent_of[path]
            parent = parent_of.get(id(ref))
            called = isinstance(parent, ast.Call) and parent.func is ref
            caller = Evaluation(repository.symbols, path, ref, {})
            target = self.target(caller, ref)
            site = f"{path}:{ref.lineno}"
            if target == "elsewhere":
                continue
            if target in noncalls:
                noncalls[target].append(site)
            elif called and not accessed:
                if coroutine and not isinstance(parent_of.get(id(parent)), ast.Await):
                    deletion.unawaited.append(site)
                else:
                    deletion.calls.append((path, parent))
                    through = self.called_through(caller, ref)
                    bound_arguments = self.bind(parent, ref, through)
                    deletion.merge(self.site_costs(caller, ref, bound_arguments))
            elif accessed and isinstance(ref, ast.Attribute):
                deletion.calls.append((path, ref))
                through = self.called_through(caller, ref)
                bound_arguments = self.bind(None, ref, through)
                deletion.merge(self.site_costs(caller, ref, bound_arguments))
            else:
                deletion.identities.append(site)
        return deletion

    def site_costs(self, caller: Evaluation, ref, bound_arguments) -> Inline:
        """What naming the owner at one call site costs: a call that cannot be
        bound to the parameters, an argument with effects the expression would
        need more than once, or a recursion the caller would enter."""
        path = caller.path
        site = f"{path}:{ref.lineno}"
        costs = Inline()
        if bound_arguments is None:
            costs.unbound.append(site)
            return costs
        uses = Counter(
            part.id
            for part in ast.walk(self.expression)
            if isinstance(part, ast.Name) and part.id in bound_arguments
        )
        costs.repeated += [
            f"{site} evaluates {ast.unparse(bound_arguments[name])} {count} times"
            for name, count in uses.items()
            if count > 1
            and not (
                isinstance(bound_arguments[name], ast.Name)
                or literal(bound_arguments[name])
            )
        ]
        enclosing = next(
            (s for s in caller.scopes if not isinstance(s, ast.Lambda)), None
        )
        body = Evaluation(
            self.repository.symbols,
            self.path,
            self.expression,
            {name: (value, caller) for name, value in bound_arguments.items()},
        )
        if isinstance(enclosing, FUNCTIONS) and any(
            any(callee is enclosing for callee in body.callees(part))
            for part in ast.walk(self.expression)
            if isinstance(part, ast.Call)
        ):
            costs.recursive.append(f"{site} in {enclosing.name}")
        return costs

    def called_through(self, caller: Evaluation, ref) -> str:
        """How a reference reaches a method: "class" when its receiver resolves
        only to classes, "instance" otherwise, "unresolved" when it does not
        resolve, "name" for a plain name."""
        if not isinstance(ref, ast.Attribute):
            return "name"
        resolution = caller.resolve(ref.value)
        if resolution is None:
            return "unresolved"
        if resolution and all(kind == "class" for kind, _ in resolution):
            return "class"
        return "instance"

    def bind(self, call, ref, through: str) -> dict[str, ast.expr] | None:
        """Each parameter's value at one call site, or None when the call cannot be
        bound statically: a spread argument, or a receiver that does not resolve
        to a class or an instance."""
        if through == "unresolved" and self.binds_receiver():
            return None
        arguments = self.node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        bound_arguments: dict[str, ast.expr] = {}
        given = list(call.args) if call else []
        keywords = list(call.keywords) if call else []
        if any(isinstance(arg, ast.Starred) for arg in given) or any(
            keyword.arg is None for keyword in keywords
        ):
            return None
        if self.binds_receiver() and positional:
            attribute_receiver = ref.value if isinstance(ref, ast.Attribute) else None
            receiver = self.receiver_argument(through, attribute_receiver, given)
            if receiver is None:
                return None
            bound_arguments[positional.pop(0).arg] = receiver
        bound_arguments |= dict(zip((p.arg for p in positional), given, strict=False))
        by_name = {keyword.arg: keyword.value for keyword in keywords}
        for parameter in (*positional, *arguments.kwonlyargs):
            if parameter.arg in by_name:
                bound_arguments[parameter.arg] = by_name.pop(parameter.arg)
        defaults = self.defaults()
        missing = [
            p.arg
            for p in (*positional, *arguments.kwonlyargs)
            if p.arg not in bound_arguments
        ]
        if any(name not in defaults for name in missing):
            return None
        bound_arguments |= {name: defaults[name] for name in missing}
        unmatched = [k for k in keywords if k.arg in by_name]
        return self.bind_rest(bound_arguments, given[len(positional) :], unmatched)

    def binds_receiver(self) -> bool:
        return (
            isinstance(self.owner, ast.ClassDef)
            and staticmethod not in self.descriptors
        )

    def receiver_argument(
        self, through: str, receiver, given: list[ast.expr]
    ) -> ast.expr | None:
        """What the first parameter binds: the attribute's receiver when it is an
        instance, or for a classmethod; the first argument when a plain method
        is called through a class or by its name."""
        if receiver is not None and (
            through == "instance" or classmethod in self.descriptors
        ):
            return receiver
        return given.pop(0) if given else None

    def defaults(self) -> dict[str, ast.expr]:
        arguments = self.node.args
        positional = [p.arg for p in (*arguments.posonlyargs, *arguments.args)]
        found = dict(zip(positional[::-1], arguments.defaults[::-1], strict=False))
        found |= {
            p.arg: d
            for p, d in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
            if d is not None
        }
        return found

    def bind_rest(self, bound_arguments: dict, extra: list, unmatched: list):
        """Bind the leftover positional and keyword arguments to *args and
        **kwargs, or None when the signature has no parameter to take them."""
        arguments = self.node.args
        if (extra and not arguments.vararg) or (unmatched and not arguments.kwarg):
            return None
        if arguments.vararg:
            bound_arguments[arguments.vararg.arg] = ast.Tuple(
                elts=extra, ctx=ast.Load()
            )
        if arguments.kwarg:
            bound_arguments[arguments.kwarg.arg] = ast.Call(
                func=ast.Name(id="dict", ctx=ast.Load()), args=[], keywords=unmatched
            )
        return bound_arguments

    def unknown_facts(self) -> tuple[str, str]:
        """An export or lineage fact the engine could not decide, as a cause and
        its detail; empty when every fact is decided."""
        published = self.published()
        exported = self.exported_name()
        uncertain = self.repository.uncertain_exports
        listing_unknown = (
            exported is not None and (self.path, exported) in uncertain
        ) or (self.path, self.node.name) in uncertain
        lineage = (
            self.repository.hierarchy.incomplete(self.owner, self.node.name)
            if isinstance(self.owner, ast.ClassDef)
            else ""
        )
        causes = (
            (
                "build configuration",
                "its package's build configuration does not resolve"
                if published == "unresolved"
                else "",
            ),
            (
                "dynamic __all__",
                "an __all__ that is not a literal listing decides its export"
                if published == "dynamic" or listing_unknown
                else "",
            ),
            (
                "unresolved base",
                lineage,
            ),
        )
        return next(((cause, why) for cause, why in causes if why), ("", ""))

    def unproven(self, deletion: Inline) -> tuple[str, str]:
        """The first reason the deletion is not decided: an unknown export or
        lineage fact, a string that may name it, a call that cannot be bound."""
        facts = self.unknown_facts()
        if facts[0]:
            return facts
        causes = (
            (
                "unresolved string",
                first(deletion.unnamed, "a string at {} may name it"),
            ),
            (
                "unbound call",
                first(deletion.unbound, "the call at {} cannot be bound"),
            ),
        )
        return next(((cause, why) for cause, why in causes if why), ("", ""))

    def undecided(self, deletion: Inline) -> tuple[str, str, str]:
        """The rows after the contract and resolution rows: recursion and shared
        formula, then an unresolved receiver or an unproven fact, then hop when a
        call resolves and unreferenced when none does."""
        if deletion.recursive:
            where = deletion.recursive[0]
            return "recursion", "", f"inlining at {where} makes that function recursive"
        if len(deletion.calls) >= 2 and held(self.node, self.expression):
            return "shared formula", "", f"{len(deletion.calls)} calls share it"
        if deletion.unresolved:
            return (
                "unresolved",
                "unresolved receiver",
                f"{len(deletion.unresolved)} receiver(s) do not resolve, first at "
                f"{deletion.unresolved[0]}",
            )
        cause, reason = self.unproven(deletion)
        if cause:
            return "unresolved", cause, reason
        if deletion.calls:
            return "hop", "", ""
        return "unreferenced", "", "nothing in the repository references it"

    def formula(self) -> str | None:
        """What the function holds, spelled the same in any file: its signature
        and its statement with each parameter named by its place and each
        imported name by its source. None where it holds nothing, is not defined
        at module level, is decorated, or reads a name that is not a parameter, an
        import or a builtin."""
        if not isinstance(self.owner, ast.Module) or self.node.decorator_list:
            return None
        if not held(self.node, self.expression):
            return None
        signature = copy.deepcopy(self.node.args)
        statement = copy.deepcopy(self.node.body[-1])
        places = {}
        for place, part in enumerate(formals(signature)):
            places[part.arg] = f"<{place}>"
            part.arg, part.annotation = places[part.arg], None
        defaults = [*signature.defaults, *filter(None, signature.kw_defaults)]
        scopes = [(statement, places), *((default, {}) for default in defaults)]
        if not all(self.spelled(scope, names) for scope, names in scopes):
            return None
        kind = type(self.node).__name__
        return "|".join([kind, ast.dump(signature), ast.dump(statement)])

    def spelled(self, scope: ast.AST, places: dict[str, str]) -> bool:
        """Rewrite each name the scope reads to its place or its source; False
        when one is neither and is not a builtin, so no other file's function
        can be shown to read the same thing."""
        sources = self.repository.sources[self.path]
        bound = self.repository.bindings[self.path]
        parts = list(ast.walk(scope))
        local = {
            part.id
            for part in parts
            if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store)
        } | {part.arg for part in parts if isinstance(part, ast.arg)}
        for name in parts:
            if not isinstance(name, ast.Name) or name.id in local:
                continue
            if name.id in places:
                name.id = places[name.id]
            elif name.id in sources:
                name.id = sources[name.id]
            elif name.id in bound or "*" in bound or not hasattr(builtins, name.id):
                return False
        return True

    def decide(self) -> Decision:
        """The first row whose evidence holds, in the order the deletion test
        reads them."""
        deletion = self.inline()
        rows = (
            ("contract", "", self.contract),
            (
                "contract",
                "",
                lambda: first(deletion.identities, "{} uses it without a call"),
            ),
            ("contract", "", lambda: first(deletion.named, "a string at {} names it")),
            (
                "contract",
                "",
                lambda: first(
                    deletion.hooks, "a base class at {} calls it on its own instance"
                ),
            ),
            (
                "contract",
                "",
                lambda: first(
                    deletion.unawaited, "{} keeps its coroutine without awaiting it"
                ),
            ),
            ("not forwarding", "", lambda: first(deletion.repeated, "{}")),
            ("unresolved", "alternative binding", self.alternative),
        )
        row, cause, reason = next(
            ((row, cause, why) for row, cause, evidence in rows if (why := evidence())),
            self.undecided(deletion),
        )
        return Decision(
            path=self.path,
            line=self.node.lineno,
            qualname=qualname(self.owner, self.node),
            expression=ast.unparse(self.expression),
            row=row,
            cause=cause,
            reason=reason,
            inline=deletion,
            formula=self.formula(),
        )


def first(locations: list[str], template: str) -> str:
    return template.format(locations[0]) if locations else ""


def qualname(owner: ast.AST, node) -> str:
    if isinstance(owner, ast.ClassDef):
        return f"{owner.name}.{node.name}"
    return node.name


def decisions(repository: Repository) -> list[Decision]:
    found = []
    for path, tree in repository.trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, FUNCTIONS):
                continue
            expression = forwarded(node)
            if expression is not None:
                found.append(Deletion(repository, path, node, expression).decide())
    return restated(found, repository.trees)


def restated(found: list[Decision], trees: dict[str, ast.Module]) -> list[Decision]:
    """The decisions, with each hop and shared formula whose formula a function
    of another file also holds decided as restated, naming the others. A file
    that is a copy of another, statement for statement, is the same file."""
    owners: dict[str, list[Decision]] = defaultdict(list)
    for decision in found:
        if decision.formula is not None and decision.row != "unreferenced":
            owners[decision.formula].append(decision)
    copies: dict[str, str] = {}
    for same in owners.values():
        for decision in same:
            if decision.path not in copies:
                copies[decision.path] = ast.dump(trees[decision.path])
        for decision in same:
            others = [
                other for other in same if copies[other.path] != copies[decision.path]
            ]
            if others and decision.row in ("hop", "shared formula"):
                decision.row = "restated"
                decision.reason = ", ".join(f"{o.path}:{o.line}" for o in others)
    return found


def single_call_hops(found: list[Decision]) -> dict[str, list[str]]:
    """Per file, the functions among the decisions that own nothing of their
    own: a hop, with what it forwards to and every call site where a caller
    would name the owner instead, and a restated formula, with the other
    functions that hold it."""
    hops: dict[str, list[str]] = {}
    for decision in found:
        subject = f"line {decision.line}: {decision.qualname}"
        if decision.row == "restated":
            finding = (
                f"{subject} restates {decision.expression}, which "
                f"{decision.reason} also holds; keep one definition"
            )
        elif decision.row == "hop":
            calls = decision.inline.calls
            modules = len({path for path, _ in calls})
            sites = ", ".join(f"{path}:{call.lineno}" for path, call in calls)
            finding = (
                f"{subject} forwards to {decision.expression}; called at "
                f"{len(calls)} site(s) in {modules} module(s): {sites}"
            )
        else:
            continue
        hops.setdefault(decision.path, []).append(finding)
    return hops


def incomplete_hop_analysis(found: list[Decision]) -> dict[str, list[str]]:
    """Per file, the candidates the deletion test could not decide, each with
    its cause, so an unresolved site never reads as analysed and clean."""
    incomplete: dict[str, list[str]] = {}
    for decision in found:
        if decision.row != "unresolved":
            continue
        incomplete.setdefault(decision.path, []).append(
            f"line {decision.line}: {decision.qualname}: {decision.cause}: "
            f"{decision.reason}"
        )
    return incomplete
