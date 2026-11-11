"""What a name or an expression refers to, resolved through the scopes and
imports that bind it rather than by its spelling.

A resolution lists the targets an expression can be, each a kind and a node:

- module: a repository module, by its file;
- class, function: a repository definition;
- instance: an instance of a repository class;
- outside: a module, class or function from outside the repository, or a
  builtin;
- foreign: a value whose type comes from outside the repository.

A resolution is None where the program does not state the target: a parameter
without an annotation, a call to a function without a return annotation, an
attribute no code in the repository binds, a name bound by a loop or by
unpacking.
"""

import ast
import builtins
import posixpath
import re
from collections import defaultdict
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from code_health.imports import Modules


def module_path(base: str, dotted: str, known: set[str]) -> str | None:
    """The file that defines a dotted module under a base directory."""
    stem = posixpath.join(base, *dotted.split(".")) if dotted else base
    for path in (f"{stem}.py", posixpath.join(stem, "__init__.py")):
        if path in known:
            return path
    return None


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = (ast.Module, ast.ClassDef, *FUNCTIONS, ast.Lambda)
BUILTIN_TYPED = (
    ast.Constant,
    ast.JoinedStr,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Compare,
)
UNIONS = {"Optional", "Union"}
QUALIFIERS = {
    "Annotated",
    "ClassVar",
    "Final",
    "Required",
    "NotRequired",
    "ReadOnly",
    "type",
    "Type",
}
PROPERTIES = {"property", "cached_property"}
DOTTED = re.compile(r"[A-Za-z_][\w.]*")
OUTSIDE = ("outside", None)
FOREIGN = ("foreign", None)


def terminal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return terminal(node.value)
    return None


def parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {
        id(child): node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }


def parameter_names(node: ast.AST) -> list[str]:
    signature = getattr(node, "args", None)
    if signature is None:
        return []
    names = [
        argument.arg
        for argument in (*signature.posonlyargs, *signature.args, *signature.kwonlyargs)
    ]
    names += [
        argument.arg for argument in (signature.vararg, signature.kwarg) if argument
    ]
    return names


def binds(scope: ast.AST, name: str) -> bool:
    """The scope binds the name itself: a parameter, an assignment, a
    definition, an import or a capture, and no global or nonlocal declaration
    hands the name to an enclosing scope."""
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    nodes = [*body]
    for statement in body:
        if not isinstance(statement, SCOPES):
            nodes += local_nodes(statement)
    if any(
        isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names
        for node in nodes
    ):
        return False
    if name in parameter_names(scope):
        return True
    return any(
        (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
        or any(bound == name for bound, _ in node_binders(node))
        for node in nodes
    )


def receiving_call(node: ast.Name, parent_of: dict[int, ast.AST]) -> ast.Call | None:
    """The call the use is handed to as an argument, keyword, spread or literal
    element of an argument; None when the use reads the value."""
    parent = parent_of.get(id(node))
    grand = parent_of.get(id(parent))
    if isinstance(parent, ast.Call) and node in parent.args:
        return parent
    if isinstance(parent, (ast.keyword, ast.Starred)) and isinstance(grand, ast.Call):
        return grand
    if isinstance(parent, (ast.Dict, ast.Tuple, ast.List)):
        if isinstance(grand, ast.Call) and parent in grand.args:
            return grand
        great = parent_of.get(id(grand))
        if isinstance(grand, ast.keyword) and isinstance(great, ast.Call):
            return great
    return None


def enclosing_scope(node: ast.AST, parent_of: dict[int, ast.AST]) -> ast.AST:
    """The scope the node is evaluated in: a nested scope's decorators,
    bases, defaults and annotations belong to the scope around it."""
    child, scope = node, parent_of[id(node)]
    while not isinstance(scope, ast.Module):
        body = scope.body if isinstance(scope, SCOPES) else None
        if child is body or (isinstance(body, list) and child in body):
            return scope
        child, scope = scope, parent_of[id(scope)]
    return scope


def local_nodes(scope: ast.AST):
    """The nodes that run in the scope itself, nested scopes' bodies excluded."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, SCOPES):
            pending.extend(ast.iter_child_nodes(node))


def union(resolutions) -> list | None:
    found = []
    for resolution in resolutions:
        if resolution is None:
            return None
        found += resolution
    return found


def instances(resolution: list | None) -> list | None:
    """What a value annotated with these types, or returned by calling these
    classes, is."""
    if resolution is None:
        return None
    found = []
    for kind, node in resolution:
        if kind == "class":
            found.append(("instance", node))
        elif kind in ("outside", "foreign"):
            found.append(FOREIGN)
        else:
            return None
    return found


def node_binders(node: ast.AST) -> list[tuple[str, ast.AST]]:
    """The names one node binds in its scope, each with the node a resolution
    reads: a definition, an import alias, an assigned name, or the statement
    for a global, nonlocal or except binding, which resolves to nothing."""
    if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
        return [(node.name, node)]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [
            ((alias.asname or alias.name).split(".")[0], alias) for alias in node.names
        ]
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return [(node.id, node)]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return [(name, node) for name in node.names]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [(node.name, node)]
    return []


def enclosing_class(scopes: list[ast.AST]) -> ast.ClassDef | None:
    """The class whose method holds a node, through any closures."""
    for inner, outer in zip(scopes, scopes[1:], strict=False):
        if isinstance(inner, FUNCTIONS) and isinstance(outer, ast.ClassDef):
            return outer
    return None


class Symbols:
    """The repository's bindings, resolved on demand and remembered."""

    def __init__(self, trees, parent_of, modules: "Modules"):
        self.trees = trees
        self.parent_of = parent_of
        self.modules = modules
        self.lineage: dict[int, list[ast.ClassDef]] = {}
        self.members: dict[tuple, list | None] = {}
        self.files = {
            id(node): path
            for path, tree in trees.items()
            for node in ast.walk(tree)
            if isinstance(node, SCOPES)
        }
        self.scope_binders: dict[int, dict[str, list[ast.AST]]] = {}
        self.named: dict[tuple[int, str], list | None] = {}
        self.class_members: dict[tuple[int, str], list | None] = {}
        self.resolving: set[tuple[int, str]] = set()

    def bases(self, node: ast.ClassDef) -> list[ast.ClassDef]:
        """The repository classes a class's base expressions resolve to."""
        defining = Evaluation(self, self.files[id(node)], node, {})
        found = []
        for base in node.bases:
            expression = base.value if isinstance(base, ast.Subscript) else base
            resolution = defining.resolve(expression)
            found += [parent for kind, parent in resolution or [] if kind == "class"]
        return found

    def ancestors(self, node: ast.ClassDef) -> list[ast.ClassDef]:
        """Every repository class a class inherits from, through resolved bases."""
        if id(node) in self.lineage:
            return self.lineage[id(node)]
        found: list[ast.ClassDef] = []
        self.lineage[id(node)] = found
        seen = {id(node)}
        pending = [node]
        while pending:
            for parent in self.bases(pending.pop()):
                if id(parent) not in seen:
                    seen.add(id(parent))
                    found.append(parent)
                    pending.append(parent)
        return found

    def member(self, receiver: list, name: str) -> list | None:
        """What an attribute of each receiver is; None when any does not resolve.
        Remembered per receiver and name."""
        key = (
            tuple(
                (kind, node if isinstance(node, str) else id(node))
                for kind, node in receiver
            ),
            name,
        )
        if key in self.members:
            return self.members[key]
        found: list | None = []
        for kind, node in receiver:
            if kind == "module":
                attribute = self.module_member(node, name)
            elif kind in ("class", "instance"):
                attribute = self.class_member(node, name)
            elif kind == "outside":
                attribute = [("outside", f"{node}.{name}")] if node else [OUTSIDE]
            else:
                attribute = None
            if attribute is None:
                found = None
                break
            found += attribute
        self.members[key] = found
        return found

    def scopes(self, path: str, node: ast.AST) -> list[ast.AST]:
        """The scopes enclosing a node, innermost first."""
        parent_of = self.parent_of[path]
        found = []
        current = parent_of.get(id(node))
        while current is not None:
            if isinstance(current, SCOPES):
                found.append(current)
            current = parent_of.get(id(current))
        return found

    def binders(self, scope: ast.AST) -> dict[str, list[ast.AST]]:
        """The nodes that bind each name in one scope."""
        if id(scope) in self.scope_binders:
            return self.scope_binders[id(scope)]
        found: dict[str, list[ast.AST]] = defaultdict(list)
        if isinstance(scope, (*FUNCTIONS, ast.Lambda)):
            signature = scope.args
            for argument in (
                *signature.posonlyargs,
                *signature.args,
                signature.vararg,
                *signature.kwonlyargs,
                signature.kwarg,
            ):
                if argument is not None:
                    found[argument.arg].append(argument)
        for node in local_nodes(scope):
            for name, binder in node_binders(node):
                found[name].append(binder)
        self.scope_binders[id(scope)] = dict(found)
        return self.scope_binders[id(scope)]

    def bound(self, scope: ast.AST, name: str) -> list | None:
        """What a name one scope binds is, across every binding of it."""
        key = (id(scope), name)
        if key in self.named:
            return self.named[key]
        if key in self.resolving:
            return None
        self.resolving.add(key)
        path = self.files[id(scope)]
        resolution = union(
            self.binder(path, binder) for binder in self.binders(scope)[name]
        )
        self.resolving.discard(key)
        self.named[key] = resolution
        return resolution

    def parameter_decided(self, function, argument: ast.arg) -> bool:
        """A method's first parameter is decided: its class's instance, or the
        class for a classmethod. Any other parameter is whatever a caller
        passes."""
        if not isinstance(function, FUNCTIONS):
            return False
        positional = [*function.args.posonlyargs, *function.args.args]
        path = self.files[id(function)]
        owner = self.scopes(path, function)[0]
        decorators = {terminal(d) for d in function.decorator_list}
        return (
            isinstance(owner, ast.ClassDef)
            and bool(positional)
            and positional[0] is argument
            and "staticmethod" not in decorators
        )

    def bound_before(self, tree: ast.Module, name: str, line: int) -> list | None:
        """What a module-scope name is when a statement at a line reads it while
        the module loads: the last binding above that line."""
        path = self.files[id(tree)]
        earlier = [b for b in self.binders(tree)[name] if b.lineno < line]
        if not earlier:
            return self.starred(path, name)
        return self.binder(path, max(earlier, key=lambda binding: binding.lineno))

    def binder(self, path: str, node: ast.AST) -> list | None:
        """What one binding gives its name: a definition, a method read as a
        property, a parameter, an import, or the value or annotation of an
        assignment."""
        if isinstance(node, ast.ClassDef):
            return [("class", node)]
        if isinstance(node, FUNCTIONS) and any(
            terminal(d) in PROPERTIES for d in node.decorator_list
        ):
            return self.returned(node)
        if isinstance(node, FUNCTIONS):
            return [("function", node)]
        if isinstance(node, ast.arg):
            return self.parameter(path, node)
        if isinstance(node, ast.alias):
            return self.imported(path, node)
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return None
        statement = self.parent_of[path].get(id(node))
        if isinstance(statement, ast.AnnAssign):
            annotated = Evaluation(self, path, statement, {})
            return instances(annotated.annotation(statement.annotation))
        if (
            isinstance(statement, ast.Assign) and node in statement.targets
        ) or isinstance(statement, ast.NamedExpr):
            return Evaluation(self, path, statement, {}).resolve(statement.value)
        return None

    def parameter(self, path: str, argument: ast.arg) -> list | None:
        """An annotated parameter is an instance of its annotation; a method's
        first parameter is its class's instance, or the class for a
        classmethod."""
        function = self.parent_of[path][id(self.parent_of[path][id(argument)])]
        if argument.annotation is not None:
            annotated = Evaluation(self, path, function, {})
            return instances(annotated.annotation(argument.annotation))
        if not isinstance(function, FUNCTIONS):
            return None
        positional = [*function.args.posonlyargs, *function.args.args]
        owner = self.scopes(path, function)[0]
        if not (positional and positional[0] is argument):
            return None
        if not isinstance(owner, ast.ClassDef):
            return None
        decorators = {terminal(d) for d in function.decorator_list}
        if "staticmethod" in decorators:
            return None
        if "classmethod" in decorators:
            return [("class", owner)]
        return [("instance", owner)]

    def imported(self, path: str, alias: ast.alias) -> list | None:
        statement = self.parent_of[path][id(alias)]
        if isinstance(statement, ast.Import):
            dotted = alias.name if alias.asname else alias.name.split(".")[0]
            found = [
                module
                for base in self.modules.bases(path)
                if base is not None
                and (module := module_path(base, dotted, self.modules.known))
            ]
            return [("module", found[0])] if found else [("outside", dotted)]
        module, name = self.modules.imported(path, statement)[
            statement.names.index(alias)
        ]
        if module is None:
            outside = ("outside", f"{statement.module}.{alias.name}")
            return None if statement.level else [outside]
        if not name:
            return [("module", module)]
        return self.module_member(module, name)

    def module_member(self, module: str, name: str) -> list | None:
        """What a name a module binds is: its own binding, a submodule of a
        package, a name a star import brings in, or a builtin."""
        tree = self.trees[module]
        if name in self.binders(tree):
            return self.bound(tree, name)
        if module.endswith("__init__.py"):
            submodule = module_path(posixpath.dirname(module), name, self.modules.known)
            if submodule:
                return [("module", submodule)]
        return self.starred(module, name)

    def starred(self, path: str, name: str) -> list | None:
        """A module-scope name no statement binds: one a star import brings in,
        or a builtin."""
        binders = self.binders(self.trees[path])
        for alias in binders["*"] if "*" in binders else []:
            statement = self.parent_of[path][id(alias)]
            module, _ = self.modules.imported(path, statement)[0]
            if module is None:
                return None
            if name in self.modules.exports(module):
                return self.module_member(module, name)
        return [("outside", f"builtins.{name}")] if hasattr(builtins, name) else None

    def class_member(self, node: ast.ClassDef, name: str) -> list | None:
        """What an attribute of a class or its instance is: the first class in
        its lineage that binds the name, in its body or through an assignment
        to the instance in one of its methods."""
        key = (id(node), name)
        if key in self.class_members:
            return self.class_members[key]
        self.class_members[key] = None
        for owner in (node, *self.ancestors(node)):
            binders = self.binders(owner)
            bound_here = binders[name] if name in binders else []
            assigned = instance_assignments(owner, name)
            if bound_here or assigned:
                path = self.files[id(owner)]
                resolution = union(
                    self.binder(path, binder) for binder in [*bound_here, *assigned]
                )
                self.class_members[key] = resolution
                return resolution
        return None

    def returned(self, function) -> list | None:
        if function.returns is None:
            return None
        defining = Evaluation(self, self.files[id(function)], function, {})
        return instances(defining.annotation(function.returns))


def instance_assignments(owner: ast.ClassDef, name: str) -> list[ast.Attribute]:
    """Each assignment of the name to the instance in the class's methods."""
    found = []
    for method in owner.body:
        if not isinstance(method, FUNCTIONS):
            continue
        positional = [*method.args.posonlyargs, *method.args.args]
        if not positional:
            continue
        receiver = positional[0].arg
        found += [
            node
            for node in local_nodes(method)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and node.attr == name
            and isinstance(node.value, ast.Name)
            and node.value.id == receiver
        ]
    return found


class Evaluation:
    """Expressions evaluated where one node sits: in its file and enclosing
    scopes, with parameters bound to expressions evaluated at another node."""

    def __init__(
        self,
        symbols: Symbols,
        path: str,
        node: ast.AST,
        bound_arguments: dict[str, tuple[ast.expr, "Evaluation"]] | None = None,
    ):
        self.symbols = symbols
        self.path = path
        self.scopes = symbols.scopes(path, node)
        load_time = isinstance(node, ast.ClassDef) and isinstance(
            self.scopes[0], ast.Module
        )
        self.load_line = node.lineno if load_time else None
        self.bound_arguments = {} if bound_arguments is None else bound_arguments

    def resolve(self, expression: ast.expr) -> list | None:
        if isinstance(expression, ast.Name):
            return self.name(expression.id)
        if isinstance(expression, ast.Attribute):
            return self.attribute(expression)
        if isinstance(expression, ast.Call):
            return self.called(expression)
        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self.resolve(expression.value)
        if isinstance(expression, ast.BoolOp):
            return union(self.resolve(value) for value in expression.values)
        if isinstance(expression, ast.IfExp):
            return union(
                self.resolve(branch) for branch in (expression.body, expression.orelse)
            )
        if isinstance(expression, BUILTIN_TYPED):
            return [FOREIGN]
        return None

    def name(self, name: str) -> list | None:
        if name in self.bound_arguments:
            expression, origin = self.bound_arguments[name]
            return origin.resolve(expression)
        for depth, scope in enumerate(self.scopes):
            if isinstance(scope, ast.ClassDef) and depth:
                continue
            if name in self.symbols.binders(scope):
                if self.load_line is not None and isinstance(scope, ast.Module):
                    return self.symbols.bound_before(scope, name, self.load_line)
                return self.symbols.bound(scope, name)
            if isinstance(scope, ast.Module):
                return self.symbols.starred(self.path, name)
        return None

    def binding_scope(self, name: str) -> ast.AST | None:
        """The scope whose binding a name reads here; None for a builtin or a
        name nothing binds."""
        for depth, scope in enumerate(self.scopes):
            if isinstance(scope, ast.ClassDef) and depth:
                continue
            if name in self.symbols.binders(scope):
                return scope
        return None

    def attribute(self, expression: ast.Attribute) -> list | None:
        receiver = self.resolve(expression.value)
        if receiver is None:
            return None
        return self.symbols.member(receiver, expression.attr)

    def annotation(self, node: ast.expr) -> list | None:
        """The types an annotation names, as the classes or outside types they
        are."""
        if isinstance(node, ast.Constant) and node.value is None:
            return []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if not DOTTED.fullmatch(node.value):
                return None
            head, *rest = node.value.split(".")
            reference: ast.expr = ast.Name(id=head, ctx=ast.Load())
            for part in rest:
                reference = ast.Attribute(value=reference, attr=part, ctx=ast.Load())
            return self.resolve(reference)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self.resolve(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return union(self.annotation(side) for side in (node.left, node.right))
        if not isinstance(node, ast.Subscript):
            return None
        head = terminal(node.value)
        arguments = (
            node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        )
        if head in UNIONS:
            return union(self.annotation(argument) for argument in arguments)
        if head in QUALIFIERS:
            return self.annotation(arguments[0])
        return self.annotation(node.value)

    def called(self, call: ast.Call) -> list | None:
        if (
            isinstance(call.func, ast.Name)
            and call.func.id == "super"
            and self.name("super") == [("outside", "builtins.super")]
        ):
            owner = enclosing_class(self.scopes)
            if owner is None:
                return None
            ancestors = self.symbols.ancestors(owner)
            return [("instance", a) for a in ancestors] if ancestors else [FOREIGN]
        callee = self.resolve(call.func)
        if callee is None:
            return None
        found = []
        for kind, node in callee:
            if kind == "class":
                produced = [("instance", node)]
            elif kind == "function":
                produced = self.symbols.returned(node)
            elif kind == "outside":
                produced = [FOREIGN]
            else:
                produced = None
            if produced is None:
                return None
            found += produced
        return found

    def callees(self, call: ast.Call) -> list[ast.AST]:
        """The repository functions a call names; none where its target does
        not resolve."""
        callee = self.resolve(call.func)
        if callee is None:
            return []
        return [node for kind, node in callee if kind == "function"]
