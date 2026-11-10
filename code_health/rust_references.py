"""Uses of function names across Rust files, read from the tree-sitter tree.

A use is a call when its expression is the function of a call expression,
turbofish included; a method-style use is the field of a called field
expression, a qualified one the name of a path, and anything else bare. Field
reads, inner path segments, use declarations, definitions, macro names,
labels and lifetimes are not uses. Inside macro input, which the tree leaves
as tokens, the neighbouring tokens decide the same. Test code counts.
"""

from dataclasses import dataclass
from typing import Literal

from tree_sitter import Node, Query, QueryCursor

from code_health.rust_source import RustFile
from code_health.rust_syntax import (
    GLOBAL_PATH,
    PATH_SEGMENTS,
    RUST_PARSER,
    enclosing_impl_type,
    path_segments,
    text,
)

DEFINITIONS = {
    "function_item": "name",
    "function_signature_item": "name",
    "macro_definition": "name",
    "macro_invocation": "macro",
    "mod_item": "name",
    "const_item": "name",
    "static_item": "name",
    "enum_variant": "name",
    "extern_crate_declaration": "name",
}
NOT_USES = frozenset({"lifetime", "label", "attribute", "scoped_type_identifier"})
EXPRESSION_STARTS = frozenset({"(", "[", "{", ",", ";", "=", "=>"})
PATH_PREFIXES = frozenset({"scoped_identifier", "scoped_type_identifier"})
ANGLE_DEPTH = {"<": 1, ">": -1, ">>": -2}
SELF_RECEIVER = ("self",)


@dataclass(frozen=True)
class RustReference:
    """One use of a name: a method on a receiver (qualifier ("self",) when
    the receiver is self), the name of a path after its qualifier, or bare;
    with the type of the impl block around it."""

    name: str
    start: int
    call: bool
    access: Literal["method", "path", "bare"]
    qualifier: tuple[str, ...]
    self_type: str | None


class ReferenceFinder:
    """Every use of one of the names in a file, read from a fresh parse of it;
    files that never spell a name are not parsed again."""

    def __init__(self, names: set[str]):
        listed = " ".join(f'"{name}"' for name in sorted(names))
        self.names = sorted(name.encode() for name in names)
        self.query = Query(
            RUST_PARSER.language,
            f"((identifier) @name (#any-of? @name {listed}))"
            f"((field_identifier) @name (#any-of? @name {listed}))",
        )

    def find(self, parsed: RustFile) -> list[RustReference]:
        if not any(name in parsed.source for name in self.names):
            return []
        tree = RUST_PARSER.parse(parsed.source)
        captured = QueryCursor(self.query).captures(tree.root_node)
        found = []
        for node in captured.get("name", []):
            if node.parent is None or inside_use(node):
                continue
            if node.parent.type == "token_tree":
                shape = token_use(token(node, False), token(node, True))
            else:
                shape = tree_use(node, node.parent)
            if shape is not None:
                access, qualifier, call = shape
                found.append(
                    RustReference(
                        text(node),
                        node.start_byte,
                        call,
                        access,
                        qualifier,
                        enclosing_impl_type(node),
                    )
                )
        return sorted(found, key=lambda reference: reference.start)


Shape = tuple[Literal["method", "path", "bare"], tuple[str, ...], bool]


def inside_use(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.type == "use_declaration":
            return True
        current = current.parent
    return False


def is_field(parent: Node, field: str, node: Node) -> bool:
    named = parent.child_by_field_name(field)
    return named is not None and named.id == node.id


def called(node: Node) -> bool:
    """Whether the expression is the function a call calls, with or without a
    turbofish."""
    parent = node.parent
    if parent is not None and parent.type == "generic_function":
        if is_field(parent, "function", node):
            node, parent = parent, parent.parent
    return (
        parent is not None
        and parent.type == "call_expression"
        and is_field(parent, "function", node)
    )


def qualifier_segments(path: Node | None, scoped: Node) -> tuple[str, ...]:
    if path is None:
        return GLOBAL_PATH if scoped.children[0].type == "::" else ()
    if path.type == "generic_type":
        path = path.child_by_field_name("type")
    if path is not None and path.type == "scoped_type_identifier":
        path = path.child_by_field_name("name")
    if path is not None and path.type in {"type_identifier", "self"}:
        return (text(path),)
    segments = path_segments(path) if path is not None else None
    return segments if segments is not None else ()


def tree_use(node: Node, parent: Node) -> Shape | None:
    if parent.type == "field_expression":
        if not called(parent):
            return None
        receiver = parent.child_by_field_name("value")
        on_self = receiver is not None and receiver.type == "self"
        return "method", SELF_RECEIVER if on_self else (), True
    if node.type == "field_identifier" or parent.type in NOT_USES:
        return None
    if parent.type == "scoped_identifier":
        grand = parent.parent
        if not is_field(parent, "name", node) or grand is None:
            return None
        if grand.type in PATH_PREFIXES or grand.type == "macro_invocation":
            return None
        path = parent.child_by_field_name("path")
        return "path", qualifier_segments(path, parent), called(parent)
    defined = DEFINITIONS.get(parent.type)
    if defined is not None and is_field(parent, defined, node):
        return None
    return "bare", (), called(node)


def token(node: Node | None, forward: bool) -> Node | None:
    current = node
    while current is not None:
        current = current.next_sibling if forward else current.prev_sibling
        if current is None or not current.is_extra:
            return current
    return None


def opens(group: Node | None, opener: str) -> bool:
    return (
        group is not None
        and group.type == "token_tree"
        and group.children[0].type == opener
    )


def turbofish_call(opener: Node) -> bool:
    """Whether the tokens from an opening angle bracket are generic arguments
    and then a parenthesised argument list."""
    depth = 0
    current: Node | None = opener
    while current is not None and current.type != ";":
        depth += ANGLE_DEPTH.get(current.type, 0)
        if depth <= 0:
            return opens(token(current, forward=True), "(")
        current = token(current, forward=True)
    return False


def token_qualifier(separator: Node) -> tuple[str, ...]:
    """The path segments before a separator; a leading :: is its own segment,
    and a qualifier that is not a plain path is empty."""
    segments: list[str] = []
    current: Node | None = separator
    while current is not None and current.type == "::":
        segment = token(current, forward=False)
        if segment is not None and segment.type in PATH_SEGMENTS:
            segments.insert(0, text(segment))
            current = token(segment, forward=False)
            continue
        if segment is None or segment.type in EXPRESSION_STARTS:
            return (*GLOBAL_PATH, *segments)
        return ()
    return tuple(segments)


def token_use(before: Node | None, after: Node | None) -> Shape | None:
    """How a name token is used, from the tokens around it in macro input."""
    if after is not None and after.type in {"!", ":"}:
        return None
    if before is not None and before.type == "fn":
        return None
    if before is not None and before.type == "!":
        defines = token(before, forward=False)
        if defines is not None and defines.text == b"macro_rules":
            return None
    call = opens(after, "(")
    if after is not None and after.type == "::":
        following = token(after, forward=True)
        if following is None or following.type != "<":
            return None
        call = turbofish_call(following)
    if before is not None and before.type == ".":
        receiver = token(before, forward=False)
        on_self = receiver is not None and receiver.type == "self"
        return "method", SELF_RECEIVER if on_self else (), call
    if before is not None and before.type == "::":
        return "path", token_qualifier(before), call
    return "bare", (), call
