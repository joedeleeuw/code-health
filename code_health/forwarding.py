"""The candidate boundary of the deletion test: a function whose body is one
expression that calls something, has no logic of its own and forwards every
argument it passes. A permitted argument is a name or a dotted name, a literal
or a display of literals, a spread of one, or a composed call whose own
arguments are permitted; anything else transforms the argument.
"""

import ast

LOGIC = (
    ast.IfExp,
    ast.BoolOp,
    ast.Compare,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.NamedExpr,
    ast.Yield,
    ast.YieldFrom,
)


def logic(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp):
        return not (
            isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        )
    if isinstance(node, ast.UnaryOp):
        return not isinstance(node.operand, ast.Constant)
    return isinstance(node, LOGIC)


def literal(node: ast.AST) -> bool:
    """A constant, or a display of constants."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.operand, ast.Constant)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and literal(key) and literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(literal(element) for element in node.elts)
    return False


def dotted_name(node: ast.AST) -> bool:
    """A name, or attributes read from a name: a, a.b, a.b.c."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name)


def permitted(argument: ast.expr) -> bool:
    """An argument the candidate forwards rather than transforms."""
    if isinstance(argument, (ast.Starred, ast.Await)):
        return permitted(argument.value)
    if isinstance(argument, ast.Call):
        return forwards(argument)
    return literal(argument) or dotted_name(argument)


def forwards(call: ast.Call) -> bool:
    return all(permitted(argument) for argument in call.args) and all(
        permitted(keyword.value) for keyword in call.keywords
    )


def documented(node) -> bool:
    first = node.body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def rooted_in_call(expression: ast.expr) -> bool:
    """One call or a composition of calls: a call, an awaited call, an attribute
    or item of a call's result, or a format string around one."""
    current = expression.value if isinstance(expression, ast.Await) else expression
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    if isinstance(current, (ast.JoinedStr, ast.BinOp)):
        return any(isinstance(part, ast.Call) for part in ast.walk(current))
    return isinstance(current, ast.Call)


def forwarded(node) -> ast.expr | None:
    """The one expression the function's body evaluates, when it is a candidate."""
    body = node.body[1:] if documented(node) else node.body
    if len(body) != 1 or not isinstance(body[0], (ast.Return, ast.Expr)):
        return None
    expression = body[0].value
    if expression is None or not rooted_in_call(expression):
        return None
    parts = list(ast.walk(expression))
    if any(logic(part) for part in parts):
        return None
    if not all(forwards(part) for part in parts if isinstance(part, ast.Call)):
        return None
    return expression


def formals(signature: ast.arguments) -> list[ast.arg]:
    """The function's own parameters, in the order its signature binds them."""
    return [
        *signature.posonlyargs,
        *signature.args,
        *filter(None, [signature.vararg]),
        *signature.kwonlyargs,
        *filter(None, [signature.kwarg]),
    ]


def held(node, expression: ast.expr) -> bool:
    """Whether deleting the function would copy something to each caller: its
    signature binds a default, it is async and awaits nothing, or its expression
    is more than one call, to a dotted name, that hands on the function's own
    parameters, each once, and returns what it gets."""
    signature = node.args
    if signature.defaults or any(signature.kw_defaults):
        return True
    if isinstance(node, ast.AsyncFunctionDef) and not isinstance(expression, ast.Await):
        return True
    call = expression.value if isinstance(expression, ast.Await) else expression
    if not isinstance(call, ast.Call) or not dotted_name(call.func):
        return True
    own = {part.arg for part in formals(signature)}
    passed = [*call.args, *(keyword.value for keyword in call.keywords)]
    handed_on = [
        argument.value if isinstance(argument, ast.Starred) else argument
        for argument in passed
    ]
    names = [argument.id for argument in handed_on if isinstance(argument, ast.Name)]
    return (
        len(names) != len(handed_on)
        or len(set(names)) != len(names)
        or not own.issuperset(names)
    )
