"""Scope questions over a Python syntax tree that several checks ask: each
node's parent, a function's parameter names, whether a nested scope rebinds a
name, and the call a name is handed to.
"""

import ast

LITERALS = (ast.Dict, ast.Tuple, ast.List)
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }


def parameter_names(node) -> list[str]:
    signature = node.args
    names = [
        argument.arg
        for argument in (*signature.posonlyargs, *signature.args, *signature.kwonlyargs)
    ]
    names += [
        argument.arg for argument in (signature.vararg, signature.kwarg) if argument
    ]
    return names


def binds(scope, name: str) -> bool:
    """The nested scope rebinds the name: a parameter or a direct assignment."""
    if isinstance(scope, FUNCTIONS) and name in parameter_names(scope):
        return True
    body = scope.body if isinstance(scope.body, list) else [scope.body]

    def visit(node) -> bool:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, NESTED):
                continue
            if (
                isinstance(child, ast.Name)
                and child.id == name
                and isinstance(child.ctx, ast.Store)
            ):
                return True
            if isinstance(child, ast.NamedExpr) and child.target.id == name:
                return True
            if visit(child):
                return True
        return False

    return any(visit(statement) for statement in body)


def receiving_call(node: ast.Name, parent_of: dict) -> ast.Call | None:
    """The call the use is handed to as an argument, keyword, spread or literal
    element of an argument; None when the use reads the value."""
    parent = parent_of.get(id(node))
    grand = parent_of.get(id(parent))
    if isinstance(parent, ast.Call) and node in parent.args:
        return parent
    if isinstance(parent, (ast.keyword, ast.Starred)) and isinstance(grand, ast.Call):
        return grand
    if isinstance(parent, LITERALS):
        if isinstance(grand, ast.Call) and parent in grand.args:
            return grand
        great = parent_of.get(id(grand))
        if isinstance(grand, ast.keyword) and isinstance(great, ast.Call):
            return great
    return None
