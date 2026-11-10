"""Rust syntax facts read from tree-sitter-rust nodes.

Test scope, struct layout contracts, canonical type text, function size
metrics, the one call a forwarding body makes and use-declaration bindings, all
decided from node kinds. Nothing outside the Rust source modules sees a node.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise

from tree_sitter import Language, Node, Parser
import tree_sitter_rust

RUST_LANGUAGE = Language(tree_sitter_rust.language())
RUST_PARSER = Parser(RUST_LANGUAGE)
COMMENTS = frozenset({"line_comment", "block_comment"})
ATTRIBUTE_ITEMS = frozenset({"attribute_item", "inner_attribute_item"})
ATTRIBUTE_OWNERS = frozenset(
    {"match_arm", "field_initializer", "shorthand_field_initializer"}
)
CONTROL_NODES = frozenset(
    "if_expression for_expression while_expression "
    "loop_expression match_expression".split()
)
BRANCH_NODES = frozenset(
    "if_expression for_expression while_expression "
    "loop_expression try_expression".split()
)
OPAQUE_NODES = frozenset({"macro_definition", "token_tree"})
MEASURED_ITEMS = frozenset({"function_item", "struct_item"})
ITEM_KEYWORDS = {"fn": "function_item", "struct": "struct_item"}
BODIED_ITEMS = frozenset({"mod_item", "impl_item", "trait_item", "function_item"})
UNMEASURED_BODIES = frozenset({"function_item", "closure_expression"})
CALL_WRAPPERS = frozenset({"try_expression", "await_expression"})
PATH_SEGMENTS = frozenset({"identifier", "crate", "self", "super"})
GLOBAL_PATH = ("::",)
FOREIGN_LAYOUT_REPRS = frozenset({b"C", b"transparent"})
FOREIGN_MARKERS = frozenset({b"ffi", b"bindgen"})
CLI_DERIVES = frozenset({b"Args", b"Parser", b"Subcommand"})
TEST_CFG_OPERATORS = {b"all": any, b"any": all}
STRING_LITERALS = frozenset({"string_literal", "raw_string_literal", "char_literal"})
NUMBER_LITERALS = frozenset({"integer_literal", "float_literal"})
MINIMUM_CLONE_LINES = 8


def text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def cfg_predicates(token_tree: Node) -> list[list[Node]]:
    parts: list[list[Node]] = [[]]
    for token in token_tree.children[1:-1]:
        if token.type == ",":
            parts.append([])
        elif not token.is_extra:
            parts[-1].append(token)
    return [part for part in parts if part]


def cfg_requires_test(predicate: list[Node]) -> bool:
    match predicate:
        case [atom] if atom.type == "identifier":
            return atom.text == b"test"
        case [operator, group] if group.type == "token_tree":
            combine = TEST_CFG_OPERATORS.get(operator.text)
            parts = cfg_predicates(group)
            return (
                combine is not None
                and bool(parts)
                and combine(cfg_requires_test(part) for part in parts)
            )
    return False


def is_test_attribute(attribute_item: Node) -> bool:
    attribute = next(
        (child for child in attribute_item.named_children if child.type == "attribute"),
        None,
    )
    if attribute is None:
        return False
    path = attribute.named_children[0]
    last = (
        path.child_by_field_name("name") if path.type == "scoped_identifier" else path
    )
    if last is not None and last.text == b"test":
        return True
    arguments = attribute.child_by_field_name("arguments")
    if path.text != b"cfg" or arguments is None:
        return False
    predicates = cfg_predicates(arguments)
    return len(predicates) == 1 and cfg_requires_test(predicates[0])


def impl_target(impl_item: Node) -> str | None:
    target = impl_item.child_by_field_name("type")
    if target is not None and target.type == "generic_type":
        target = target.child_by_field_name("type")
    if target is not None and target.type == "scoped_type_identifier":
        target = target.child_by_field_name("name")
    if target is None or target.type != "type_identifier":
        return None
    return text(target)


def enclosing_impl_type(node: Node) -> str | None:
    """The type of the impl block the node is in, through closures and macro
    input; None outside one or for a type it cannot name."""
    current = node.parent
    while current is not None and current.type != "impl_item":
        current = current.parent
    return impl_target(current) if current is not None else None


def tests_container(container: Node) -> bool:
    """Whether the inner attributes that open a file or body require test."""
    for child in container.named_children:
        if child.type == "inner_attribute_item" and is_test_attribute(child):
            return True
        if child.type not in COMMENTS and child.type != "inner_attribute_item":
            return False
    return False


@dataclass(frozen=True)
class Scoped:
    node: Node
    attributes: tuple[Node, ...]


def leading_attributes(node: Node) -> list[Node]:
    if node.type not in ATTRIBUTE_OWNERS:
        return []
    return [child for child in node.named_children if child.type == "attribute_item"]


def tests_body(node: Node) -> bool:
    body = node.child_by_field_name("body") if node.type in BODIED_ITEMS else None
    return body is not None and tests_container(body)


def scoped_children(node: Node) -> Iterator[Scoped]:
    """Named children with their outer attributes, leaving out test-scoped ones:
    under a test attribute, or an item whose body has an inner one."""
    pending: list[Node] = []
    for child in node.named_children:
        if child.type in COMMENTS or child.type == "inner_attribute_item":
            continue
        if child.type == "attribute_item":
            pending.append(child)
            continue
        attributes = (*pending, *leading_attributes(child))
        pending = []
        if not any(is_test_attribute(attribute) for attribute in attributes):
            if not tests_body(child):
                yield Scoped(child, attributes)


def attribute_marks(attribute_item: Node) -> Iterator[tuple[bytes, Node]]:
    stack = [attribute_item]
    while stack:
        current = stack.pop()
        children = current.children
        for head, arguments in pairwise(children):
            if head.type == "identifier" and arguments.type == "token_tree":
                yield head.text, arguments
        stack.extend(child for child in children if child.child_count)


def token_texts(token_tree: Node) -> set[bytes]:
    found: set[bytes] = set()
    stack = [token_tree]
    while stack:
        current = stack.pop()
        if current.type == "identifier":
            found.add(current.text)
        stack.extend(current.children)
    return found


def contract_layout(attributes: tuple[Node, ...]) -> bool:
    """Whether an attribute hands the struct's layout to a foreign ABI, generated
    bindings or a clap command line, so its shape is not first-party."""
    for attribute in attributes:
        if FOREIGN_MARKERS & token_texts(attribute):
            return True
        for head, arguments in attribute_marks(attribute):
            listed = token_texts(arguments)
            if head == b"repr" and FOREIGN_LAYOUT_REPRS & listed:
                return True
            if head == b"derive" and CLI_DERIVES & listed:
                return True
    return False


def canonical_type(type_node: Node) -> str:
    """Type text without lifetimes or comments, spaced only between words."""
    tokens: list[str] = []
    stack = [type_node]
    while stack:
        current = stack.pop()
        if current.type == "lifetime" or current.type in COMMENTS:
            continue
        if current.child_count == 0:
            tokens.append(text(current))
            continue
        stack.extend(reversed(current.children))
    joined = ""
    for token in tokens:
        if (
            joined
            and (joined[-1].isalnum() or joined[-1] == "_")
            and (token[0].isalnum() or token[0] == "_")
        ):
            joined += " "
        joined += token
    return joined


def only_lifetimes(struct_item: Node) -> bool:
    generics = struct_item.child_by_field_name("type_parameters")
    return generics is None or all(
        child.type == "lifetime_parameter"
        for child in generics.named_children
        if child.type not in COMMENTS
    )


def named_fields(struct_item: Node) -> tuple[tuple[str, str], ...] | None:
    """A named-field struct's production fields and canonical types, sorted."""
    declarations = struct_item.child_by_field_name("body")
    if declarations is None or declarations.type != "field_declaration_list":
        return None
    if declarations.has_error:
        return None
    fields = []
    for scoped in scoped_children(declarations):
        name = scoped.node.child_by_field_name("name")
        field_type = scoped.node.child_by_field_name("type")
        if scoped.node.type == "field_declaration" and name and field_type:
            fields.append((text(name), canonical_type(field_type)))
    return tuple(sorted(fields))


def match_arm_count(match_expr: Node) -> int:
    return sum(
        1
        for block in match_expr.named_children
        if block.type == "match_block"
        for arm in scoped_children(block)
        if arm.node.type == "match_arm"
    )


def is_boolean_branch(expression: Node) -> bool:
    if expression.type != "binary_expression":
        return False
    operator = expression.child_by_field_name("operator")
    return operator is not None and operator.text in (b"&&", b"||")


def branch_weight(current: Node) -> int:
    if current.type in BRANCH_NODES or is_boolean_branch(current):
        return 1
    if current.type == "match_expression":
        return match_arm_count(current)
    if current.type == "match_arm":
        pattern = current.child_by_field_name("pattern")
        guarded = pattern is not None and pattern.child_by_field_name("condition")
        return int(guarded is not None)
    return 0


def body_metrics(function_item: Node) -> tuple[int, int]:
    """Complexity and control-flow nesting of a function body, without the
    bodies of nested functions and closures or test-scoped code."""
    complexity = 1
    max_nesting = 0
    body = function_item.child_by_field_name("body")
    children = list(scoped_children(body)) if body is not None else []
    stack = [(scoped.node, 0) for scoped in reversed(children)]
    while stack:
        current, depth = stack.pop()
        if current.type in OPAQUE_NODES or current.type in UNMEASURED_BODIES:
            continue
        complexity += branch_weight(current)
        else_if = (
            current.type == "if_expression"
            and current.parent is not None
            and current.parent.type == "else_clause"
        )
        next_depth = depth + int(current.type in CONTROL_NODES and not else_if)
        max_nesting = max(max_nesting, next_depth)
        stack.extend(
            (scoped.node, next_depth)
            for scoped in reversed(list(scoped_children(current)))
        )
    return complexity, max_nesting


def body_clone_key(function_item: Node) -> str | None:
    """Canonical token sequence of a multi-statement function body spanning at
    least eight non-comment lines, with string and numeric literals masked."""
    body = function_item.child_by_field_name("body")
    if body is None or body.has_error:
        return None
    statements = list(scoped_children(body))
    if len(statements) < 2:
        return None
    rows: set[int] = set()
    tokens: list[str] = []
    stack = [scoped.node for scoped in reversed(statements)]
    while stack:
        current = stack.pop()
        if current.type in COMMENTS or current.type in ATTRIBUTE_ITEMS:
            continue
        if current.type in STRING_LITERALS:
            rows.update(range(current.start_point.row, current.end_point.row + 1))
            tokens.append('"')
            continue
        if current.type in NUMBER_LITERALS:
            rows.add(current.start_point.row)
            tokens.append("0")
            continue
        if current.child_count == 0:
            rows.add(current.start_point.row)
            tokens.append(text(current))
            continue
        stack.extend(reversed(current.children))
    return " ".join(tokens) if len(rows) >= MINIMUM_CLONE_LINES else None


def parameter_count(function_item: Node) -> int:
    declared = function_item.child_by_field_name("parameters")
    if declared is None:
        return 0
    return sum(
        1
        for scoped in scoped_children(declared)
        if scoped.node.type in {"parameter", "variadic_parameter"}
    )


def bound_name(pattern: Node) -> str | None:
    """The name a parameter pattern binds when it binds exactly one, mutably or
    by reference included; raw identifiers are identifiers."""
    while pattern.type in {"ref_pattern", "mut_pattern"}:
        pattern = pattern.named_children[-1]
    return text(pattern) if pattern.type == "identifier" else None


def parameter_names(function_item: Node) -> tuple[bool, tuple[str, ...]] | None:
    """Whether the function takes self, and its other parameters' names in
    order; None when a parameter binds a pattern, nothing or a rest."""
    receiver = False
    names = []
    declared = function_item.child_by_field_name("parameters")
    for scoped in scoped_children(declared) if declared else ():
        pattern = scoped.node.child_by_field_name("pattern")
        if scoped.node.type == "self_parameter" or (
            pattern is not None and pattern.type == "self"
        ):
            receiver = True
            continue
        name = bound_name(pattern) if scoped.node.type == "parameter" else None
        if pattern is None or name is None:
            return None
        names.append(name)
    return receiver, tuple(names)


@dataclass(frozen=True)
class ForwardedCall:
    """A callee named by a plain path of identifiers or a method on self."""

    segments: tuple[str, ...]
    on_self: bool

    @property
    def text(self) -> str:
        return ("." if self.on_self else "::").join(self.segments)


def path_segments(path: Node) -> tuple[str, ...] | None:
    """A path of plain segments; a leading :: is its own segment."""
    if path.type in PATH_SEGMENTS:
        return (text(path),)
    if path.type != "scoped_identifier":
        return None
    qualifier = path.child_by_field_name("path")
    name = path.child_by_field_name("name")
    if qualifier is None:
        head = GLOBAL_PATH if path.children[0].type == "::" else None
    else:
        head = path_segments(qualifier)
    if head is None or name is None:
        return None
    return (*head, text(name))


def callee(function: Node) -> ForwardedCall | None:
    if function.type == "field_expression":
        receiver = function.child_by_field_name("value")
        field = function.child_by_field_name("field")
        if receiver is None or receiver.type != "self" or field is None:
            return None
        return ForwardedCall(("self", text(field)), True)
    segments = path_segments(function)
    return ForwardedCall(segments, False) if segments else None


def code_children(node: Node) -> list[Node]:
    return [child for child in node.named_children if child.type not in COMMENTS]


def sole_call(statements: list[Scoped]) -> Node | None:
    """The call expression that is a body's one attribute-free production
    statement, optionally returned, awaited or propagated with a question
    mark."""
    if len(statements) != 1 or statements[0].attributes:
        return None
    expression = statements[0].node
    if expression.type == "expression_statement":
        expression = code_children(expression)[0]
    if expression.type == "return_expression":
        returned = code_children(expression)
        if not returned:
            return None
        expression = returned[0]
    while expression.type in CALL_WRAPPERS:
        expression = code_children(expression)[0]
    return expression if expression.type == "call_expression" else None


def forwarded_call(function_item: Node) -> ForwardedCall | None:
    """The callee when the body is one sole call that passes every parameter
    once, in order: the named ones as its arguments and self, when taken, as
    the receiver of a method on self."""
    signature = parameter_names(function_item)
    body = function_item.child_by_field_name("body")
    if signature is None or body is None or not signature[1]:
        return None
    receiver, names = signature
    expression = sole_call(list(scoped_children(body)))
    if expression is None:
        return None
    function = expression.child_by_field_name("function")
    arguments = expression.child_by_field_name("arguments")
    if function is None or arguments is None:
        return None
    passed = [scoped.node for scoped in scoped_children(arguments)]
    if any(argument.type != "identifier" for argument in passed):
        return None
    if tuple(text(argument) for argument in passed) != names:
        return None
    call = callee(function)
    return call if call is not None and call.on_self == receiver else None


def grouped_bindings(
    clause: Node, prefix: tuple[str, ...]
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """The bindings of a braced use list or a glob under its path."""
    path = clause.child_by_field_name("path") or next(
        (child for child in clause.named_children if child.type != "use_list"),
        None,
    )
    head = path_segments(path) if path is not None else ()
    if head is None:
        return
    listed = clause.child_by_field_name("list")
    if clause.type == "use_wildcard":
        yield "*", prefix + head
    elif listed is not None:
        yield from use_bindings(listed, prefix + head)


def use_bindings(
    clause: Node, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Each name a use clause binds with the path it stands for; a glob
    yields * with the module it opens."""
    if clause.type == "use_list":
        for member in code_children(clause):
            yield from use_bindings(member, prefix)
        return
    if clause.type in {"scoped_use_list", "use_wildcard"}:
        yield from grouped_bindings(clause, prefix)
        return
    alias = None
    if clause.type == "use_as_clause":
        alias = clause.child_by_field_name("alias")
        clause = clause.child_by_field_name("path")
    segments = path_segments(clause) if clause is not None else None
    if segments is None:
        return
    segments = prefix + segments
    if segments[-1] == "self" and len(segments) > 1:
        segments = segments[:-1]
    binding = text(alias) if alias is not None else segments[-1]
    if binding != "_":
        yield binding, segments


class TestScope:
    """Whether a node lies in test-scoped code: anywhere in a test file, or
    under an ancestor that scoped_children leaves out."""

    def __init__(self, whole: bool):
        self.whole = whole
        self.kept: dict[int, set[int]] = {}

    def __call__(self, node: Node) -> bool:
        if self.whole:
            return True
        current = node
        while current.parent is not None:
            parent = current.parent
            if parent.id not in self.kept:
                self.kept[parent.id] = {
                    scoped.node.id for scoped in scoped_children(parent)
                }
            if (
                current.is_named
                and current.id not in self.kept[parent.id]
                and current.type not in COMMENTS
                and current.type not in ATTRIBUTE_ITEMS
            ):
                return True
            current = parent
        return False


def hidden_item(error: Node) -> str | None:
    """The kind of item whose code an ERROR or MISSING node hides: the struct
    or function around it, else one whose keyword the ERROR swallowed."""
    current = error.parent
    while current is not None:
        if current.type in MEASURED_ITEMS:
            return current.type
        current = current.parent
    if not error.is_error:
        return None
    keywords: set[str] = set()
    stack = [error]
    while stack:
        current = stack.pop()
        if current.child_count == 0:
            keywords.add(current.type)
        stack.extend(current.children)
    return next(
        (item for keyword, item in ITEM_KEYWORDS.items() if keyword in keywords), None
    )


def unread_lines(root: Node, test_scope: TestScope) -> tuple[list[int], list[int]]:
    """The lines of outermost ERROR and MISSING nodes that hide a production
    struct's fields, and those that hide a function's code."""
    structs: set[int] = set()
    functions: set[int] = set()
    stack = [root] if root.has_error else []
    while stack:
        current = stack.pop()
        if not (current.is_error or current.is_missing):
            stack.extend(child for child in current.children if child.has_error)
            continue
        line = current.start_point.row + 1
        hidden = hidden_item(current)
        if hidden == "function_item":
            functions.add(line)
        elif hidden == "struct_item" and not test_scope(current):
            structs.add(line)
    return sorted(structs), sorted(functions)
