"""Private single-call wrapper hops in Rust source.

A candidate is a private, attribute-free function other than main, outside
trait and trait impl bodies and extern ABIs, whose production body is one call
that passes each parameter once, in order (self as the receiver of a method on
self), optionally returned, awaited or propagated with a question mark. The
callee must be first-party: a method of the function's own inherent impl type
on self or Self, a free function of the same module, or a path through crate,
super, self, an imported name or a module of the crate that names a free
function of the crate. Paths rooted at an external crate (a leading ::, the
sysroot crates and the dependencies the crate's manifest declares), paths with
a capitalized type segment, and calls to a function of the same name are not.
A candidate is a hop when the code that can see it, its own module and the
modules inside it, names it at least once as a call and never as a value;
tests count. A bare name is the candidate where the use declarations in scope
bind it to the candidate's module, or none binds it and it is in that module
or one a glob in scope opens; a path names it where its qualifier resolves to
that module, or for a method to Self or its impl type.
"""

from collections import defaultdict
from itertools import dropwhile

from code_health.rust_crates import RustCrates
from code_health.rust_references import SELF_RECEIVER, ReferenceFinder, RustReference
from code_health.rust_source import RustFile, RustFunction
from code_health.rust_syntax import GLOBAL_PATH, ForwardedCall

PATH_ROOTS = frozenset({"crate", "super", "self"})
Module = tuple[str, ...]


def is_super(segment: str) -> bool:
    return segment == "super"


def modules_named(
    parsed: RustFile,
    position: int,
    segments: tuple[str, ...],
    seen: frozenset[tuple[int, str]] = frozenset(),
) -> set[Module]:
    """Each module a path of segments can name at a position: through crate,
    self or super, an import of its first segment, or else relative to the
    module, to the crate root or to a glob in scope."""
    if not segments or segments[0] == GLOBAL_PATH[0]:
        return set()
    module = parsed.module_at(position)
    head, *rest = segments
    if head == "crate":
        return {tuple(rest)}
    if head == "self":
        return {(*module, *rest)}
    if head == "super":
        supers = len(segments) - len(tuple(dropwhile(is_super, segments)))
        if supers > len(module):
            return set()
        return {(*module[: len(module) - supers], *segments[supers:])}
    scope = parsed.uses_at(position, production=False)
    bound = scope.names.get(head)
    if bound is not None:
        declared, path = bound
        if (declared.start, head) in seen:
            return set()
        imported = modules_named(
            parsed, declared.start, path, seen | {(declared.start, head)}
        )
        return {(*found, *rest) for found in imported}
    found = {(*module, *segments), segments}
    for declared, base in scope.globs:
        found |= {
            (*opened, *segments)
            for opened in modules_named(parsed, declared.start, base, seen)
        }
    return found


def names_method(candidate: RustFunction, reference: RustReference) -> bool:
    if reference.access == "method":
        if reference.qualifier == SELF_RECEIVER:
            return reference.self_type == candidate.impl_type
        return True
    if reference.access == "bare" or not reference.qualifier:
        return False
    if reference.qualifier == ("Self",):
        return reference.self_type == candidate.impl_type
    return reference.qualifier[-1] == candidate.impl_type


def refers_to(
    candidate: RustFunction, parsed: RustFile, reference: RustReference
) -> bool:
    if parsed.path == candidate.path:
        if candidate.span[0] <= reference.start < candidate.span[1]:
            return False
    module = parsed.module_at(reference.start)
    if module[: len(candidate.module_path)] != candidate.module_path:
        return False
    if candidate.impl_type is not None:
        return names_method(candidate, reference)
    if reference.access == "method":
        return False
    if reference.access == "path":
        named = modules_named(parsed, reference.start, reference.qualifier)
        return candidate.module_path in named
    scope = parsed.uses_at(reference.start, production=False)
    bound = scope.names.get(candidate.name)
    if bound is not None:
        declared, path = bound
        return path[-1] == candidate.name and candidate.module_path in modules_named(
            parsed, declared.start, path[:-1]
        )
    return module == candidate.module_path or any(
        candidate.module_path in modules_named(parsed, declared.start, base)
        for declared, base in scope.globs
    )


class RustHops:
    """The single-call hops of a workspace's Rust files and the name tables
    that decide whether a forwarded call is first-party."""

    def __init__(self, files: list[RustFile], crates: RustCrates):
        self.files = files
        self.crates = crates
        self.crate_of: dict[str, str] = {}
        self.free_fns: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
        self.impl_methods: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.crate_fns: dict[str, set[str]] = defaultdict(set)
        self.crate_modules: dict[str, set[str]] = defaultdict(set)
        self.by_path: dict[str, RustFile] = {}
        for parsed in files:
            self.add(parsed)

    def add(self, parsed: RustFile) -> None:
        crate = parsed.crate
        self.crate_of[parsed.path] = crate
        for function in parsed.functions:
            if function.impl_type is None:
                self.free_fns[(parsed.path, function.module_path)].add(function.name)
                self.crate_fns[crate].add(function.name)
            else:
                self.impl_methods[(crate, function.impl_type)].add(function.name)
        self.crate_modules[crate].update(parsed.modules)
        self.by_path[parsed.path] = parsed

    def own_method(self, function: RustFunction, method: str) -> bool:
        crate = self.crate_of[function.path]
        return (
            function.impl_type is not None
            and method in self.impl_methods[(crate, function.impl_type)]
        )

    def resolved(self, function: RustFunction, segments: tuple[str, ...]):
        """The path with leading imported names replaced by what they bind, and
        whether one was; None for an import cycle."""
        parsed = self.by_path[function.path]
        position = function.span[0]
        imported = False
        seen: set[str] = set()
        while bound := parsed.uses_at(position, True).names.get(segments[0]):
            head = segments[0]
            if head in seen:
                return None
            seen.add(head)
            imported = True
            declared, replacement = bound
            position = declared.start
            if replacement[0] == head:
                break
            segments = replacement + segments[1:]
        return segments, imported

    def first_party(self, function: RustFunction, call: ForwardedCall) -> bool:
        target = call.segments[-1]
        if target == function.name:
            return False
        if call.on_self:
            return self.own_method(function, target)
        crate = self.crate_of[function.path]
        external = self.crates.external(crate)
        root = call.segments[0]
        if root == "Self":
            return len(call.segments) == 2 and self.own_method(function, target)
        if root == GLOBAL_PATH[0]:
            return False
        if len(call.segments) > 1 and (root in external or root[:1].isupper()):
            return False
        resolution = self.resolved(function, call.segments)
        if resolution is None:
            return False
        segments, imported = resolution
        if len(segments) == 1:
            return not imported and call.segments[0] in self.free_fns.get(
                (function.path, function.module_path), set()
            )
        root, *_, target = segments
        if (
            root in external
            or root == GLOBAL_PATH[0]
            or target == function.name
            or any(part[:1].isupper() for part in segments[:-1])
        ):
            return False
        return target in self.crate_fns[crate] and (
            root in PATH_ROOTS or root in self.crate_modules[crate]
        )

    def candidates(self) -> list[RustFunction]:
        """Private attribute-free functions other than main whose body forwards
        their parameters, in order, to a first-party callee."""
        return [
            function
            for parsed in self.files
            for function in parsed.functions
            if function.forwards is not None
            and not function.public
            and not function.has_attrs
            and function.name != "main"
            and self.first_party(function, function.forwards)
        ]

    def reference_counts(
        self, candidates: list[RustFunction]
    ) -> dict[tuple[str, int], tuple[int, int]]:
        """Call sites and value references of each candidate, by path and line."""
        if not candidates:
            return {}
        by_name: dict[str, list[RustFunction]] = defaultdict(list)
        for candidate in candidates:
            by_name[candidate.name].append(candidate)
        finder = ReferenceFinder(set(by_name))
        counts = {(candidate.path, candidate.line): [0, 0] for candidate in candidates}
        for parsed in self.files:
            for reference in finder.find(parsed):
                for candidate in by_name[reference.name]:
                    if self.crate_of[candidate.path] != parsed.crate:
                        continue
                    if refers_to(candidate, parsed, reference):
                        slot = counts[(candidate.path, candidate.line)]
                        slot[0 if reference.call else 1] += 1
        return {key: (pair[0], pair[1]) for key, pair in counts.items()}

    def findings(self) -> dict[str, list[str]]:
        """Each candidate with a call site and no value reference, by file."""
        candidates = self.candidates()
        counts = self.reference_counts(candidates)
        hops: dict[str, list[str]] = {}
        for function in candidates:
            call_sites, value_refs = counts[(function.path, function.line)]
            if call_sites >= 1 and value_refs == 0 and function.forwards is not None:
                hops.setdefault(function.path, []).append(
                    f"line {function.line}: {function.name} -> "
                    f"{function.forwards.text} ({call_sites} call site(s))"
                )
        return hops
