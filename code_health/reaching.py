"""Whether a name, or a chain of attributes read from one, is decided by a
single binding that reaches its use unconditionally.

A name reaches its use through the one binding of its scope that every path
to the use runs: a statement directly in the scope's body, not inside a
conditional, loop, try or other block. Where a scope binds the name more than
once, a use while the scope loads reads the last such binding above it; a use
at any other time needs exactly one. A del of the name, a global or nonlocal
declaration of it, a star import beside it, or a binding inside a block leaves
no single reaching binding. A parameter decides nothing about its value except
a method's first parameter, which is its class's instance or the class. An
attribute of a repository module or class reaches through that module's or
class's own single binding; an attribute of something outside the repository
is that thing's attribute, and a call of a decided repository class is an
instance of it.
"""

import ast
import builtins

from code_health.symbols import FUNCTIONS, Evaluation, Symbols, local_nodes


class Reaching:
    """Single reaching bindings in one repository."""

    def __init__(self, symbols: Symbols):
        self.symbols = symbols
        self.globals_declared = {
            path: {
                name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Global, ast.Nonlocal))
                for name in node.names
            }
            for path, tree in symbols.trees.items()
        }

    def certain(self, evaluation: Evaluation, expression: ast.expr) -> bool:
        if isinstance(expression, ast.Subscript):
            return self.certain(evaluation, expression.value)
        if isinstance(expression, ast.Name):
            return self.name(evaluation, expression.id)
        if isinstance(expression, ast.Call):
            made = evaluation.resolve(expression.func)
            return (
                bool(made)
                and all(kind == "class" for kind, _ in made)
                and self.certain(evaluation, expression.func)
            )
        if not isinstance(expression, ast.Attribute):
            return False
        if not self.certain(evaluation, expression.value):
            return False
        receiver = evaluation.resolve(expression.value)
        return receiver is not None and all(
            self.member(kind, node, expression.attr) for kind, node in receiver
        )

    def name(self, evaluation: Evaluation, name: str) -> bool:
        if name in evaluation.bound_arguments:
            return False
        scope = evaluation.binding_scope(name)
        if scope is None:
            module = evaluation.scopes[-1]
            return (
                hasattr(builtins, name)
                and "*" not in self.symbols.binders(module)
                and name not in self.globals_declared[evaluation.path]
            )
        binding = self.reaching(scope, name, evaluation.load_line)
        path = self.symbols.files[id(scope)]
        return binding is not None and self.decided(path, binding)

    def reaching(self, scope: ast.AST, name: str, load_line: int | None):
        """The single binding of a name that reaches its use, or None."""
        path = self.symbols.files[id(scope)]
        bindings = self.symbols.binders(scope)[name]
        if isinstance(scope, ast.Module) and name in self.globals_declared[path]:
            return None
        if "*" in self.symbols.binders(scope) and isinstance(scope, ast.Module):
            return None
        if any(
            isinstance(node, ast.Name)
            and node.id == name
            and isinstance(node.ctx, ast.Del)
            for node in local_nodes(scope)
        ):
            return None
        if not all(self.unconditional(scope, binding) for binding in bindings):
            return None
        if load_line is not None and isinstance(scope, ast.Module):
            earlier = [b for b in bindings if b.lineno < load_line]
            return max(earlier, key=lambda b: b.lineno) if earlier else None
        return bindings[0] if len(bindings) == 1 else None

    def unconditional(self, scope: ast.AST, binding: ast.AST) -> bool:
        """The binding is made by a statement directly in the scope's body, as
        a definition, an import, a parameter or a plain assignment target."""
        if isinstance(binding, ast.arg):
            return True
        parent_of = self.symbols.parent_of[self.symbols.files[id(scope)]]
        statement = binding if isinstance(binding, ast.stmt) else parent_of[id(binding)]
        body = scope.body if isinstance(scope.body, list) else []
        if not any(statement is top for top in body):
            return False
        if isinstance(binding, ast.Name):
            return isinstance(statement, ast.Assign) and binding in statement.targets
        return isinstance(binding, (ast.alias, *FUNCTIONS, ast.ClassDef))

    def decided(self, path: str, binding: ast.AST) -> bool:
        """The binding decides what the name is: a definition, an import, a
        method's first parameter, or an assignment of a decided expression."""
        if isinstance(binding, (*FUNCTIONS, ast.ClassDef)):
            return True
        symbols = self.symbols
        statement = symbols.parent_of[path][id(binding)]
        if isinstance(binding, ast.arg):
            function = symbols.parent_of[path][id(statement)]
            return symbols.parameter_decided(function, binding)
        if isinstance(binding, ast.alias):
            return self.imported(path, statement, binding)
        return self.certain(Evaluation(symbols, path, statement, {}), statement.value)

    def imported(self, path: str, statement, alias: ast.alias) -> bool:
        if isinstance(statement, ast.Import):
            return True
        module, name = self.symbols.modules.imported(path, statement)[
            statement.names.index(alias)
        ]
        if module is None:
            return not statement.level
        return not name or self.member("module", module, name)

    def member(self, kind: str, node, attribute: str) -> bool:
        if kind == "outside":
            return True
        if kind == "module":
            tree = self.symbols.trees[node]
            if attribute in self.symbols.binders(tree):
                binding = self.reaching(tree, attribute, None)
                return binding is not None and self.decided(node, binding)
            return self.symbols.module_member(node, attribute) is not None and (
                "*" not in self.symbols.binders(tree)
            )
        if kind in ("class", "instance"):
            for owner in (node, *self.symbols.ancestors(node)):
                if attribute in self.symbols.binders(owner):
                    binding = self.reaching(owner, attribute, None)
                    path = self.symbols.files[id(owner)]
                    return binding is not None and self.decided(path, binding)
        return False
