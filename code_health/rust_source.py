"""Typed records of a Rust source file, read from its tree-sitter parse.

Named-field structs, functions and methods and function size metrics, with
test scope left out: a test file, an item, field, statement or argument under
a test attribute or a cfg that requires test, and a module, impl, function or
file whose inner attributes require test. Generated files are not read.
Inline modules and use declarations are recorded everywhere, test code
included, with the span they are visible in; a module's path is its file's
place in the crate's module tree followed by the inline modules around it.
An outermost syntax error is recorded by line when it hides a production
struct's fields or a function's code. The input of a macro in item position,
or of a brace-delimited macro anywhere, is read as items where it parses as
items; its functions get no size metrics.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re

from tree_sitter import Node, Parser, Query, QueryCursor, Range, Tree

from code_health import stop
from code_health.rust_crates import RustCrates
from code_health.rust_syntax import (
    OPAQUE_NODES,
    RUST_LANGUAGE,
    RUST_PARSER,
    ForwardedCall,
    Scoped,
    TestScope,
    body_clone_key,
    body_metrics,
    contract_layout,
    forwarded_call,
    impl_target,
    named_fields,
    only_lifetimes,
    parameter_count,
    scoped_children,
    tests_container,
    text,
    unread_lines,
    use_bindings,
)

TEST_FILE = re.compile(
    r"(^|/)(tests?|benches|examples|__tests__|e2e|fixtures|testdata|tests/ui)/"
    r"|(^|/)tests?\.rs$|_(test|tests|bench)(/|\.rs$)"
)
GENERATED_FILE = re.compile(r"(^|/)(gen|generated|target|vendor|third_party)/")
GENERATED_HEADER = re.compile(
    r"@generated|\bauto-?generated\b|\bdo not edit\b",
    re.IGNORECASE,
)
ITEM_CONTAINERS = frozenset({"source_file", "declaration_list"})
CRATE_ROOTS = frozenset({"lib", "main"})
SCOPES = Query(
    RUST_LANGUAGE,
    "(mod_item name: (identifier) @name body: (declaration_list)) @module"
    "(use_declaration argument: (_) @clause) @use",
)


@dataclass(frozen=True)
class RustStruct:
    path: str
    line: int
    name: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RustFunction:
    path: str
    line: int
    name: str
    module_path: tuple[str, ...]
    impl_type: str | None
    public: bool
    has_attrs: bool
    forwards: ForwardedCall | None
    span: tuple[int, int]
    clone_key: str | None


@dataclass(frozen=True)
class RustFunctionSize:
    name: str
    complexity: int
    nesting: int
    arity: int | None


@dataclass(frozen=True)
class RustUse:
    """The names one use declaration binds, with the span of the module body,
    file or block they are visible in and the module that holds it."""

    start: int
    scope: tuple[int, int]
    module: tuple[str, ...]
    test: bool
    names: dict[str, tuple[str, ...]]
    globs: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class UseScope:
    names: dict[str, tuple[RustUse, tuple[str, ...]]]
    globs: list[tuple[RustUse, tuple[str, ...]]]


@dataclass
class RustFile:
    path: str
    crate: str
    module: tuple[str, ...]
    source: bytes
    lines: int
    production: bool
    tree: Tree
    unread_structs: list[int] = field(default_factory=list)
    unread_functions: list[int] = field(default_factory=list)
    structs: list[RustStruct] = field(default_factory=list)
    functions: list[RustFunction] = field(default_factory=list)
    sizes: list[RustFunctionSize] = field(default_factory=list)
    mod_spans: list[tuple[int, int, str]] = field(default_factory=list)
    uses: list[RustUse] = field(default_factory=list)
    modules: set[str] = field(default_factory=set)

    def module_at(self, position: int) -> tuple[str, ...]:
        inline = (name for lo, hi, name in self.mod_spans if lo <= position < hi)
        return (*self.module, *inline)

    def uses_at(self, position: int, production: bool) -> UseScope:
        """The names and globs the use declarations in scope at a position
        bind, an inner block's over its module's; test ones only when test
        code is read."""
        module = self.module_at(position)
        visible = sorted(
            (
                declared
                for declared in self.uses
                if declared.scope[0] <= position < declared.scope[1]
                and declared.module == module
                and not (production and declared.test)
            ),
            key=lambda declared: declared.scope[0] - declared.scope[1],
        )
        scope = UseScope({}, [])
        for declared in visible:
            scope.names.update(
                (name, (declared, path)) for name, path in declared.names.items()
            )
            scope.globs.extend((declared, base) for base in declared.globs)
        return scope


def owning_item(function_item: Node) -> Node | None:
    """The impl or trait whose body declares the function, if one does."""
    container = function_item.parent
    owner = container.parent if container is not None else None
    if container is None or container.type != "declaration_list" or owner is None:
        return None
    return owner if owner.type in {"impl_item", "trait_item"} else None


def has_extern_abi(function_item: Node) -> bool:
    return any(
        child.type == "function_modifiers"
        and any(modifier.type == "extern_modifier" for modifier in child.children)
        for child in function_item.named_children
    )


def macro_items(source: bytes, token_tree: Node) -> Iterator[Node]:
    """The item syntax a macro takes as input: the whole token tree when it
    parses as items, else each brace group in it that does."""
    groups = [token_tree]
    while groups:
        group = groups.pop()
        opener, closer = group.children[0], group.children[-1]
        if opener.end_byte >= closer.start_byte:
            continue
        inside = Range(
            opener.end_point, closer.start_point, opener.end_byte, closer.start_byte
        )
        fragment = Parser(RUST_LANGUAGE, included_ranges=[inside]).parse(source)
        if not fragment.root_node.has_error:
            yield fragment.root_node
            continue
        groups.extend(
            child
            for child in reversed(group.named_children)
            if child.type == "token_tree" and child.children[0].type == "{"
        )


class FileReader:
    """Records the items under a syntax node; sizes only for written code,
    so a macro's input never shifts the keys of the functions around it.
    Each reader returns whether the walk descends into the node."""

    def __init__(self, parsed: RustFile, written: bool):
        self.parsed = parsed
        self.written = written
        self.readers = {
            "struct_item": self.struct,
            "function_item": self.function,
            "mod_item": self.module,
            "macro_invocation": self.macro_input,
        }

    def walk(self, container: Node) -> None:
        stack = list(scoped_children(container))
        stack.reverse()
        while stack:
            scoped = stack.pop()
            if scoped.node.type in OPAQUE_NODES:
                continue
            reader = self.readers.get(scoped.node.type)
            if reader is None or reader(scoped):
                stack.extend(reversed(list(scoped_children(scoped.node))))

    def macro_input(self, scoped: Scoped) -> bool:
        node = scoped.node
        token_tree = node.named_children[-1]
        item_position = node.parent is not None and node.parent.type in ITEM_CONTAINERS
        if token_tree.type != "token_tree" or not (
            item_position or token_tree.children[0].type == "{"
        ):
            return False
        for fragment in macro_items(self.parsed.source, token_tree):
            FileReader(self.parsed, written=False).walk(fragment)
        return False

    def struct(self, scoped: Scoped) -> bool:
        node = scoped.node
        name = node.child_by_field_name("name")
        fields = named_fields(node)
        if name is None or fields is None or text(name).endswith("Builder"):
            return True
        if only_lifetimes(node) and len(fields) >= 3:
            if not contract_layout(scoped.attributes):
                line = name.start_point.row + 1
                self.parsed.structs.append(
                    RustStruct(self.parsed.path, line, text(name), fields)
                )
        return True

    def function(self, scoped: Scoped) -> bool:
        node = scoped.node
        name = node.child_by_field_name("name")
        if name is None or node.child_by_field_name("body") is None:
            return True
        owner = owning_item(node)
        implements = owner is not None and owner.child_by_field_name("trait")
        if self.written:
            complexity, nesting = body_metrics(node)
            arity = None if implements else parameter_count(node)
            self.parsed.sizes.append(
                RustFunctionSize(text(name), complexity, nesting, arity)
            )
        impl_type = impl_target(owner) if owner is not None else None
        contract = owner is not None and (
            owner.type == "trait_item" or implements or impl_type is None
        )
        if contract or has_extern_abi(node):
            return True
        self.parsed.functions.append(
            RustFunction(
                self.parsed.path,
                name.start_point.row + 1,
                text(name),
                self.parsed.module_at(node.start_byte),
                impl_type,
                any(child.type == "visibility_modifier" for child in node.children),
                bool(scoped.attributes),
                forwarded_call(node),
                (node.start_byte, node.end_byte),
                body_clone_key(node) if self.written else None,
            )
        )
        return True

    def module(self, scoped: Scoped) -> bool:
        name = scoped.node.child_by_field_name("name")
        if name is not None:
            self.parsed.modules.add(text(name))
        return scoped.node.child_by_field_name("body") is not None


def file_module(crate: str, path: str) -> tuple[str, ...]:
    """The file's module path from the crate's root: src/lib.rs and src/main.rs
    are the root, a.rs and a/mod.rs are a, and every other file under the
    package, src/bin targets included, is named by its folders and stem."""
    *folders, name = PurePosixPath(path).relative_to(crate).parts
    if folders[:1] == ["src"]:
        folders = folders[1:]
    stem = PurePosixPath(name).stem
    root = not folders or folders[:1] == ["bin"]
    if stem == "mod" or (stem in CRATE_ROOTS and root):
        return tuple(folders)
    return (*folders, stem)


def record_scopes(parsed: RustFile, root: Node, test_scope: TestScope) -> None:
    captured = QueryCursor(SCOPES).captures(root)
    modules = sorted(captured.get("module", []), key=lambda node: node.start_byte)
    for node in modules:
        name = node.child_by_field_name("name")
        if name is not None:
            parsed.mod_spans.append((node.start_byte, node.end_byte, text(name)))
    for node in captured.get("use", []):
        clause = node.child_by_field_name("argument")
        container = node.parent
        if clause is None or container is None:
            continue
        bound = list(use_bindings(clause))
        parsed.uses.append(
            RustUse(
                node.start_byte,
                (container.start_byte, container.end_byte),
                parsed.module_at(node.start_byte),
                test_scope(node),
                {name: path for name, path in bound if name != "*"},
                tuple(path for name, path in bound if name == "*"),
            )
        )


def parse_rust_file(root: Path, path: str, crates: RustCrates) -> RustFile | None:
    if GENERATED_FILE.search(path):
        return None
    try:
        source = (root / path).read_bytes()
    except OSError as error:
        stop(f"cannot read Rust source {path}: {error.strerror}")
    header = b"\n".join(source.splitlines()[:20]).decode("utf-8", errors="replace")
    if GENERATED_HEADER.search(header):
        return None
    tree = RUST_PARSER.parse(source)
    crate = crates.crate(path)
    test_scope = TestScope(
        bool(TEST_FILE.search(path)) or tests_container(tree.root_node)
    )
    parsed = RustFile(
        path,
        crate,
        file_module(crate, path),
        source,
        len(source.splitlines()),
        not test_scope.whole,
        tree,
    )
    parsed.unread_structs, parsed.unread_functions = unread_lines(
        tree.root_node, test_scope
    )
    record_scopes(parsed, tree.root_node, test_scope)
    if parsed.production:
        FileReader(parsed, written=True).walk(tree.root_node)
    return parsed
