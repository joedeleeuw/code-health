"""Python's own visibility of a module's names: the names its __all__ lists,
read from literal lists and tuples, += of literals and .extend or .append of
literals, in source order, directly in the module's body. Any other use of
the name, or one inside a conditional, function or other block, makes the
listing undecided.
"""

import ast

LISTING_METHODS = {"extend", "append"}


def assigned(node: ast.stmt) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def listed_strings(node: ast.expr | None) -> list[str] | None:
    """The strings of a literal list or tuple, None for anything else."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    if not all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in node.elts
    ):
        return None
    return [item.value for item in node.elts]


def listing_change(node: ast.stmt) -> tuple[bool, list[str] | None, bool]:
    """Whether a statement changes __all__, the literal names it adds, and
    whether it replaces the listing rather than extending it."""
    if any(isinstance(t, ast.Name) and t.id == "__all__" for t in assigned(node)):
        replaces = not isinstance(node, ast.AugAssign)
        return True, listed_strings(getattr(node, "value", None)), replaces
    call = node.value if isinstance(node, ast.Expr) else None
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "__all__"
    ):
        return False, None, False
    if call.func.attr not in LISTING_METHODS or len(call.args) != 1:
        return True, None, False
    if call.func.attr == "append":
        appended_expression = call.args[0]
        appended = isinstance(appended_expression, ast.Constant) and isinstance(
            appended_expression.value, str
        )
        return True, [appended_expression.value] if appended else None, False
    return True, listed_strings(call.args[0]), False


def all_listing(tree: ast.Module) -> tuple[set[str] | None, bool]:
    """The names a module's __all__ lists, None when it declares none, and
    whether its listing is undecided: a change to __all__ that is not a literal
    statement directly in the module's body, or any other use of the name, such
    as an item or slice write, a del, a read handed elsewhere or a use inside a
    function or block."""
    names: set[str] | None = None
    dynamic = False
    accounted: set[int] = set()
    for node in tree.body:
        changes, added, replaces = listing_change(node)
        if not changes:
            continue
        if added is None:
            dynamic = True
            continue
        names = set(added) if replaces or names is None else names | set(added)
        accounted |= {
            id(part)
            for part in ast.walk(node)
            if isinstance(part, ast.Name) and part.id == "__all__"
        }
    if any(
        isinstance(part, ast.Name)
        and part.id == "__all__"
        and id(part) not in accounted
        for part in ast.walk(tree)
    ):
        dynamic = True
    return names, dynamic
