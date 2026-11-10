"""Python instance shapes: each class's annotated instance fields, with the
canonical type of each field and whether a constructor requires it, read from
the repository's modules (code_health.restatements compares them).

A field type names classes by their resolved identity: a repository class by
its defining module and qualified name, followed through the module's imports
and re-exports; a standard-library name by its module path, with the typing
aliases folded onto the builtins and collections.abc; an external name by its
full import path. A name that resolves to nothing is unresolved. An attribute a
method stores on the instance takes the annotation its first assignment
declares, the annotation of a parameter it assigns unchanged; without one its
type is unknown.

A field is required when a constructor must be given it. A TypedDict's own key,
of a class with typing.TypedDict or a TypedDict ancestor among its bases, is
required by its Required or NotRequired qualifier, else by the class's own
total keyword; a class a base that does not resolve may make a TypedDict has
undecided requiredness wherever the two readings differ. Otherwise a field the
class's own __init__ stores from one of its parameters unchanged is required
when that parameter has no default, and any other stored attribute is not. A
class-body field without a value, or with an Ellipsis, is required; with any
other value it is a default, unless the value calls a field specifier of the
class's constructor provider, which decides by its default, default_factory or
factory and init arguments, or a positional default. The providers are
dataclasses, attrs and pydantic with their own specifiers, NamedTuple and
TypedDict with none, and a repository class or function a
typing.dataclass_transform declaration marks, with the field_specifiers it
names; a provider that does not resolve leaves undecided whether a call to a
name it may declare is a specifier, and with it the field's requiredness.

A class is one identity per module and qualified name, so a module's .pyi
stub and a conditional redefinition declare the same class. A field's owners
are the repository classes at the root of the class's ancestry that declare
it. A class-body value is class metadata, not an instance field, only when the
class's own class-body values do not become constructor fields, a repository
ancestor declares the attribute at class level and an ancestor reads it from
the class or instance, and no class in the ancestry stores it on an instance;
an ancestry that does not resolve gives no such evidence. A constructor-
generating decorator processes the class it decorates only; a provider base,
and a repository class a typing.dataclass_transform declaration marks,
process the classes deriving from them. dataclasses.KW_ONLY is a marker, not a
field. A string annotation that is not an expression leaves the class's shape
unknown.
"""

import ast
import builtins
import collections.abc
import re
from dataclasses import dataclass

from code_health.bindings import own_nodes
from code_health.class_bodies import (
    class_level_names,
    initializer_stores,
    instance_reads,
    instance_stores,
    resolved_heads,
    specifier_required,
)
from code_health.imports import STANDARD_LIBRARY, Modules

MINIMUM_FIELDS = 3
PROTOCOLS = ("typing.Protocol",)
TYPING_ALIASES = {
    "List": "list",
    "Dict": "dict",
    "Set": "set",
    "FrozenSet": "frozenset",
    "Tuple": "tuple",
    "Type": "type",
    "Text": "str",
}
ABSTRACT_COLLECTIONS = frozenset(collections.abc.__all__)
BUILTINS = frozenset(dir(builtins))
WRAPPERS = (
    "typing.Annotated",
    "typing.Required",
    "typing.NotRequired",
    "typing.ReadOnly",
)
ATTRS_FIELDS = frozenset(
    {"⟨E:attr.ib⟩", "⟨E:attr.attrib⟩", "⟨E:attr.field⟩", "⟨E:attrs.field⟩"}
)
PYDANTIC_FIELDS = frozenset({"⟨E:pydantic.Field⟩", "⟨E:pydantic.fields.Field⟩"})
PROVIDERS = {
    "dataclasses.dataclass": frozenset({"dataclasses.field"}),
    "typing.NamedTuple": frozenset(),
    "typing.TypedDict": frozenset(),
    **dict.fromkeys(
        (
            "⟨E:attr.s⟩",
            "⟨E:attr.attrs⟩",
            "⟨E:attr.define⟩",
            "⟨E:attr.frozen⟩",
            "⟨E:attr.mutable⟩",
            "⟨E:attr.dataclass⟩",
            "⟨E:attrs.define⟩",
            "⟨E:attrs.frozen⟩",
            "⟨E:attrs.mutable⟩",
        ),
        ATTRS_FIELDS,
    ),
    "⟨E:pydantic.BaseModel⟩": PYDANTIC_FIELDS,
    "⟨E:pydantic.main.BaseModel⟩": PYDANTIC_FIELDS,
    "⟨E:pydantic.dataclasses.dataclass⟩": PYDANTIC_FIELDS | {"dataclasses.field"},
}
GENERATED_CONSTRUCTORS = frozenset(
    {
        "dataclasses.dataclass",
        "attr.s",
        "attr.attrs",
        "attr.define",
        "attr.frozen",
        "attr.mutable",
        "attrs.define",
        "attrs.frozen",
        "attrs.mutable",
        "pydantic.dataclasses.dataclass",
        "typing.NamedTuple",
        "typing.TypedDict",
        "typing.dataclass_transform",
    }
)
PLAIN_BASES = frozenset({"object", "abc.ABC", "typing.Generic", "typing.Protocol"})
TYPE_FORMS = frozenset({"type", "typing.Type", "typing_extensions.Type"})
TOKEN = re.compile(r"⟨([REU]):([^⟩]*)⟩")
UNKNOWN_TYPE = "⟨?⟩"
ANY = "⟨A⟩"
BARE_GENERICS = frozenset(
    {"list", "dict", "set", "frozenset", "tuple", "type"}
    | {f"collections.abc.{name}" for name in ABSTRACT_COLLECTIONS}
    - {"collections.abc.Hashable", "collections.abc.Sized"}
)


@dataclass(frozen=True)
class ShapedClass:
    path: str
    module: str
    qualname: str
    node: ast.ClassDef
    protocol: bool
    bases: frozenset[str]
    inherited: frozenset[str]
    fields: tuple

    @property
    def identity(self) -> str:
        return f"{self.module}.{self.qualname}"

    def __str__(self) -> str:
        return f"{self.path}::{self.qualname}:{self.node.lineno}"


@dataclass
class DeclaredClass:
    path: str
    module: str
    qualname: str
    node: ast.ClassDef
    types: "FieldTypes"
    bases: list[str]
    decorators: list[str]
    names: frozenset[str]
    class_level: frozenset[str]
    reads: frozenset[str]
    writes: frozenset[str]


class Unreadable(Exception):
    """A string annotation that is not a Python expression: the field's type,
    and so the class's shape, is unknown."""


def annotation_names(path: str, module: str, tree: ast.Module) -> dict[str, str]:
    """The dotted name each name the module imports outside functions and
    classes stands for, type-checking blocks included."""
    package = module.split(".")
    if not path.endswith(("__init__.py", "__init__.pyi")):
        package.pop()
    imports = {}
    for statement in own_nodes(tree):
        if isinstance(statement, ast.ImportFrom):
            source = statement.module or ""
            if statement.level:
                base = package[: len(package) - statement.level + 1]
                source = ".".join((*base, source)).rstrip(".")
            imports.update(
                (alias.asname or alias.name, f"{source}.{alias.name}")
                for alias in statement.names
            )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.asname:
                    imports[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    imports[head] = head
    return imports


def qualified_classes(scope: ast.AST, prefix: str = "", annotations=None):
    """Each class under the scope with its qualified name and the parameter
    annotations of the functions enclosing it, innermost binding last."""
    annotations = annotations or {}
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ClassDef):
            yield node, prefix + node.name, annotations
            yield from qualified_classes(node, f"{prefix}{node.name}.", annotations)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            own = {
                argument.arg: argument.annotation
                for argument in ast.walk(node.args)
                if isinstance(argument, ast.arg) and argument.annotation is not None
            }
            yield from qualified_classes(
                node, f"{prefix}{node.name}.<locals>.", {**annotations, **own}
            )
        else:
            pending.extend(ast.iter_child_nodes(node))


def forward_reference(text: str) -> ast.expr:
    """The expression a string annotation names."""
    try:
        return ast.parse(text.strip(), mode="eval").body
    except SyntaxError as error:
        raise Unreadable(f"annotation {text!r} is not an expression") from error


class FieldTypes:
    """A field's type as one canonical text: resolved identities as tokens,
    Optional, Union and the | operator as one sorted union, Literal values
    sorted, and Annotated and the TypedDict qualifiers reduced to the type they
    wrap."""

    def __init__(
        self,
        index: "ClassIndex",
        path: str,
        module: str,
        scope: str,
        annotations: dict[str, ast.expr] | None = None,
    ):
        self.index = index
        self.path = path
        self.module = module
        self.scope = scope
        self.annotations = annotations or {}
        if path not in index.module_imports:
            index.module_imports[path] = annotation_names(
                path, module, index.modules.trees[path]
            )
        self.imports = index.module_imports[path]

    def resolve(self, chain: list[str]) -> str:
        """The canonical text of a dotted name written in the module."""
        head, *rest = chain
        if head in self.imports:
            return self.index.resolve(".".join([self.imports[head], *rest]))
        local = self.index.modules.exports(self.path)
        nested = f"{self.module}.{self.scope}{'.'.join(chain)}"
        if self.scope and nested in self.index.declared_paths:
            return f"⟨R:{nested}⟩"
        declared = f"{self.module}.{'.'.join(chain)}"
        if local.get(head) == self.path or declared in self.index.declared_paths:
            return f"⟨R:{declared}⟩"
        if head in BUILTINS and not rest:
            return head
        if not rest and (bound := self.parameter_type(head)):
            return bound
        return f"⟨U:{'.'.join(chain)}⟩"

    def parameter_type(self, name: str) -> str:
        """The class a parameter of an enclosing function annotated Type[X]
        holds, when the name is such a parameter."""
        annotation = self.annotations.get(name)
        if not isinstance(annotation, ast.Subscript):
            return ""
        if self.name(annotation.value) not in TYPE_FORMS:
            return ""
        return self.form(annotation.slice)

    def name(self, node: ast.expr) -> str:
        chain = []
        while isinstance(node, ast.Attribute):
            chain.insert(0, node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return ast.unparse(node)
        return self.resolve([node.id, *chain])

    def form(self, annotation: ast.expr) -> str:
        """The canonical name an annotation's outermost name resolves to."""
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            annotation = forward_reference(annotation.value)
        if isinstance(annotation, ast.Subscript):
            annotation = annotation.value
        if isinstance(annotation, (ast.Name, ast.Attribute)):
            return self.name(annotation)
        return ""

    @staticmethod
    def arguments(node: ast.Subscript) -> list[ast.expr]:
        if isinstance(node.slice, ast.Tuple):
            return list(node.slice.elts)
        return [node.slice]

    def text(self, node: ast.expr) -> str:
        def members(part: ast.expr) -> list[ast.expr]:
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
                return [*members(part.left), *members(part.right)]
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                return members(forward_reference(part.value))
            if isinstance(part, ast.Subscript) and isinstance(
                part.value, (ast.Name, ast.Attribute)
            ):
                base = self.name(part.value)
                arguments = self.arguments(part)
                if base == "typing.Optional":
                    return [*members(arguments[0]), ast.Constant(None)]
                if base == "typing.Union":
                    return [member for item in arguments for member in members(item)]
            return [part]

        return " | ".join(sorted({self.member(member) for member in members(node)}))

    def member(self, node: ast.expr) -> str:
        """The canonical text of one union member; a generic written without
        its arguments takes Any for them."""
        if isinstance(node, ast.Constant):
            return "..." if node.value is Ellipsis else repr(node.value)
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = self.name(node)
            return f"{name}[{ANY}]" if name in BARE_GENERICS else name
        if isinstance(node, ast.List):
            return f"[{', '.join(self.text(item) for item in node.elts)}]"
        if isinstance(node, ast.Subscript) and isinstance(
            node.value, (ast.Name, ast.Attribute)
        ):
            base = self.name(node.value)
            arguments = self.arguments(node)
            if base in WRAPPERS:
                return self.text(arguments[0])
            if base == "typing.Literal":
                literals = sorted(
                    repr(literal.value)
                    if isinstance(literal, ast.Constant)
                    else self.member(literal)
                    for literal in arguments
                )
                return f"Literal[{', '.join(literals)}]"
            return f"{base}[{', '.join(self.text(item) for item in arguments)}]"
        return ast.unparse(node)

    def required(self, annotation: ast.expr, total: bool | None) -> bool | None:
        """A TypedDict key's Required or NotRequired qualifier overrides the
        class's totality."""
        if isinstance(annotation, ast.Subscript):
            base = self.form(annotation)
            if base in ("typing.Required", "typing.NotRequired"):
                return base == "typing.Required"
            if base in ("typing.Annotated", "typing.ReadOnly"):
                return self.required(self.arguments(annotation)[0], total)
        return total

    def assigned_annotations(self, method):
        """The instance attributes a method assigns, each with its annotation,
        or None when the method does not declare it."""
        arguments = {
            argument.arg: argument.annotation
            for argument in (
                *method.args.posonlyargs,
                *method.args.args,
                *method.args.kwonlyargs,
            )
            if argument.annotation is not None
        }
        for statement in ast.walk(method):
            if isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                annotation = statement.annotation
            elif isinstance(statement, ast.Assign):
                targets = statement.targets
                annotation = arguments.get(getattr(statement.value, "id", None))
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    yield target.attr, annotation

    def method_annotations(self, node: ast.ClassDef, declared: set[str]) -> list:
        """The attributes the class's methods assign beyond the declared ones,
        each with the annotation its first assignment declares, or None, and
        whether the constructor requires it."""
        annotations: dict[str, ast.expr | None] = {}
        for method in node.body:
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for name, annotation in self.assigned_annotations(method):
                    if name not in declared:
                        annotations.setdefault(name, annotation)
        stores = initializer_stores(node)
        return [
            (name, annotation, stores.get(name, False))
            for name, annotation in annotations.items()
        ]

    def typed(self, declared: list) -> tuple:
        """The canonical fields; a field without a declared type has the unknown
        type."""
        fields: dict[str, tuple[str, bool]] = {}
        for name, annotation, required in declared:
            if annotation is None:
                fields[name] = (UNKNOWN_TYPE, required)
                continue
            try:
                fields[name] = (self.text(annotation), required)
            except Unreadable as error:
                raise Unreadable(f"field {name}: {error}") from error
        return tuple(sorted((name, *typed) for name, typed in fields.items()))


def outside_name(parts: list[str]) -> str:
    """The canonical text of a dotted path outside the repository: typing
    aliases folded onto the builtins and collections.abc, standard-library
    paths as written, anything else an external token."""
    top = parts[0]
    if top in ("typing", "typing_extensions") and len(parts) == 2:
        leaf = parts[1]
        if leaf == "Any":
            return ANY
        if leaf in TYPING_ALIASES:
            return TYPING_ALIASES[leaf]
        if leaf in ABSTRACT_COLLECTIONS:
            return f"collections.abc.{leaf}"
        return f"typing.{leaf}"
    if top == "builtins" and len(parts) == 2:
        return parts[1]
    if top in STANDARD_LIBRARY:
        return ".".join(parts)
    return f"⟨E:{'.'.join(parts)}⟩"


class ClassIndex:
    """Every class of the modules, one per class identity: a module's source
    before its stub, and of two copies of one module the shallower, which an
    absolute import finds first. For each, the field names it declares, its
    resolved bases and decorators; for each complete class of at least the
    minimum field count, its shape; and each class whose shape is unknown."""

    def __init__(self, modules: Modules):
        self.modules = modules
        ordered = sorted(
            modules.trees,
            key=lambda path: (path.endswith(".pyi"), path.count("/"), path),
        )
        self.paths: dict[str, str] = {}
        for path in ordered:
            self.paths.setdefault(modules.name(path), path)
        for alias, real in modules.aliases.items():
            self.paths.setdefault(modules.name(alias), real)
        self.declared_paths: set[str] = set()
        self.module_imports: dict[str, dict[str, str]] = {}
        for path in ordered:
            module = self.modules.name(path)
            self.declared_paths.update(
                f"{module}.{qualname}"
                for _, qualname, _ in qualified_classes(modules.trees[path])
            )
        self.classes: dict[str, DeclaredClass] = {}
        self.unreadable: list[str] = []
        self.providers: dict[str, frozenset[str]] = {}
        for path in ordered:
            self.declare(path)
            self.transforms(path)
        self.processing: dict[str, bool] = {}
        self.specifiers: dict[str, frozenset[str] | None] = {}
        self.dictionaries: dict[str, bool | None] = {}
        self.ancestors: dict[str, list[str]] = {}
        self.owned: dict[tuple[str, str], frozenset[str]] = {}
        self.shapes: list[ShapedClass] = []
        self.typed: dict[str, tuple] = {}
        for declared in self.classes.values():
            self.shape(declared)

    def resolve(self, dotted: str) -> str:
        """The canonical text of a dotted import path."""
        parts = dotted.split(".")
        for cut in range(len(parts), 0, -1):
            module = ".".join(parts[:cut])
            if module not in self.paths:
                continue
            return self.followed(self.paths[module], parts[cut:])
        if parts[0] in self.modules.packages and parts[0] not in STANDARD_LIBRARY:
            return f"⟨U:{dotted}⟩"
        return outside_name(parts)

    def followed(self, path: str, rest: list[str]) -> str:
        """The canonical text of an attribute path read from a repository
        module, followed through the modules that re-export each name: a name
        another module defines continues there, a submodule continues as that
        module, and anything else does not resolve."""
        while rest:
            name, owner = rest[0], self.modules.name(path)
            defined = f"{owner}.{'.'.join(rest)}"
            exported = self.modules.exports(path)
            if exported.get(name) == path or defined in self.declared_paths:
                return f"⟨R:{defined}⟩"
            target = exported.get(name)
            if target is None:
                return self.reexported(path, rest, defined)
            if self.modules.exports(target).get(name) == target:
                path = target
            elif self.modules.name(target) == f"{owner}.{name}":
                path, rest = target, rest[1:]
            else:
                return f"⟨U:{defined}⟩"
        return f"⟨R:{self.modules.name(path)}⟩"

    def reexported(self, path: str, rest: list[str], defined: str) -> str:
        """The canonical text of a name a repository module binds by importing
        it from outside the repository, continued along the rest of the path;
        a name it does not import does not resolve."""
        if path not in self.module_imports:
            self.module_imports[path] = annotation_names(
                path, self.modules.name(path), self.modules.trees[path]
            )
        imported = self.module_imports[path].get(rest[0])
        if imported is None:
            return f"⟨U:{defined}⟩"
        return self.resolve(".".join([imported, *rest[1:]]))

    def declare(self, path: str) -> None:
        tree = self.modules.trees[path]
        module = self.modules.name(path)
        for node, qualname, annotations in qualified_classes(tree):
            identity = f"{module}.{qualname}"
            if identity in self.classes:
                continue
            scope = qualname.rsplit(".", 1)[0] + "." if "." in qualname else ""
            types = FieldTypes(self, path, module, scope, annotations)
            try:
                bases, decorators, names = resolved_heads(node, types)
            except Unreadable as error:
                if len(class_level_names(node)) >= MINIMUM_FIELDS:
                    self.unreadable.append(f"{path}::{qualname}:{node.lineno} {error}")
                continue
            self.classes[identity] = DeclaredClass(
                path,
                module,
                qualname,
                node,
                types,
                bases,
                decorators,
                names,
                class_level_names(node),
                frozenset(instance_reads(node)),
                frozenset(instance_stores(node)),
            )

    def transforms(self, path: str) -> None:
        """Record each constructor provider a typing.dataclass_transform
        declaration in the module marks, with the field specifiers it names: a
        module-level function, or a class through itself or its
        __init_subclass__, which provides for the classes deriving from it."""
        module = self.modules.name(path)
        tree = self.modules.trees[path]
        marked = [
            (function, f"{module}.{function.name}", "")
            for function in tree.body
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node, qualname, _ in qualified_classes(tree):
            hooks = [
                member
                for member in node.body
                if isinstance(member, ast.FunctionDef)
                and member.name == "__init_subclass__"
            ]
            marked += [
                (declaration, f"{module}.{qualname}", f"{qualname}.")
                for declaration in [node, *hooks]
            ]
        for declaration, provider, scope in marked:
            types = FieldTypes(self, path, module, scope)
            for decorator in declaration.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and types.form(decorator.func) == "typing.dataclass_transform"
                ):
                    continue
                named = next(
                    (
                        k.value
                        for k in decorator.keywords
                        if k.arg == "field_specifiers"
                    ),
                    None,
                )
                elements = (
                    named.elts if isinstance(named, (ast.Tuple, ast.List)) else []
                )
                self.providers[provider] = frozenset(map(types.name, elements))

    def ancestry(self, identity: str) -> list[str]:
        """The repository classes above the class, nearest first."""
        if identity in self.ancestors:
            return self.ancestors[identity]
        found: list[str] = []
        self.ancestors[identity] = found
        pending = [identity]
        while pending:
            current = pending.pop(0)
            for base in self.classes[current].bases:
                match = TOKEN.fullmatch(base)
                if match and match[1] == "R" and match[2] in self.classes:
                    if match[2] not in found and match[2] != identity:
                        found.append(match[2])
                        pending.append(match[2])
        return found

    def generates_constructor(self, identity: str) -> bool:
        """Whether the class's annotated class-body values become constructor
        fields: a constructor-generating decorator on the class itself, or a
        base that generates constructors for the classes deriving from it."""
        declared = self.classes[identity]
        return any(
            name in GENERATED_CONSTRUCTORS or name in PROVIDERS
            for name in declared.decorators
        ) or any(self.processes_subclasses(base) for base in declared.bases)

    def processes_subclasses(self, base: str) -> bool:
        """Whether deriving from the base generates a constructor: a provider
        base, a repository class a typing.dataclass_transform declaration marks
        or that derives from such a base, or a base that does not resolve and
        may; a decorator on a repository base processes that base only."""
        match = TOKEN.fullmatch(base)
        if match and match[1] == "R" and match[2] in self.classes:
            if match[2] in self.processing:
                return self.processing[match[2]]
            self.processing[match[2]] = True
            processes = match[2] in self.providers or any(
                self.processes_subclasses(above)
                for above in self.classes[match[2]].bases
            )
            self.processing[match[2]] = processes
            return processes
        if match or base in GENERATED_CONSTRUCTORS:
            return True
        if base in PLAIN_BASES or base.split(".")[0] in STANDARD_LIBRARY:
            return False
        return base not in BUILTINS

    def class_metadata(self, identity: str, field: ast.AnnAssign) -> bool:
        """A class-body value is class metadata when the class's own class-body
        values do not become constructor fields, a repository ancestor declares
        the attribute at class level and an ancestor reads it from the class or
        instance, and no class in the ancestry stores it on an instance."""
        if field.value is None or self.generates_constructor(identity):
            return False
        name = field.target.id
        ancestors = [self.classes[a] for a in self.ancestry(identity)]
        chain = [self.classes[identity], *ancestors]
        return (
            any(name in ancestor.class_level for ancestor in ancestors)
            and any(name in ancestor.reads for ancestor in ancestors)
            and not any(name in member.writes for member in chain)
        )

    def typed_dictionary(self, identity: str) -> bool | None:
        """Whether the class is a TypedDict: typing.TypedDict or a TypedDict
        among its bases; None when a base that does not resolve may be one."""
        if identity in self.dictionaries:
            return self.dictionaries[identity]
        self.dictionaries[identity] = False
        verdicts = set()
        for base in self.classes[identity].bases:
            match = TOKEN.fullmatch(base)
            if base == "typing.TypedDict":
                verdicts.add(True)
            elif match and match[1] == "R" and match[2] in self.classes:
                verdicts.add(self.typed_dictionary(match[2]))
            elif match:
                verdicts.add(None)
        if True in verdicts:
            verdict = True
        elif None in verdicts:
            verdict = None
        else:
            verdict = False
        self.dictionaries[identity] = verdict
        return verdict

    def constructor_specifiers(self, identity: str) -> frozenset[str] | None:
        """The field specifiers of the providers that may build the class's
        constructor: those its own decorators name and those its bases hand
        down; None when a decorator or base that does not resolve, or that no
        provider declaration names, may provide one."""
        found: frozenset[str] | None = frozenset()
        for name in self.classes[identity].decorators:
            provided = self.provided(name)
            found = None if None in (found, provided) else found | provided
        inherited = self.inherited_specifiers(identity)
        return None if None in (found, inherited) else found | inherited

    def inherited_specifiers(self, identity: str) -> frozenset[str] | None:
        """The field specifiers the class's bases hand down to the classes
        deriving from them: a provider base's own, and what a repository base
        inherits in turn; a decorator on a repository base provides for that
        base only."""
        if identity in self.specifiers:
            return self.specifiers[identity]
        self.specifiers[identity] = frozenset()
        found: frozenset[str] | None = frozenset()
        for base in self.classes[identity].bases:
            match = TOKEN.fullmatch(base)
            provided = self.provided(base)
            if match and match[1] == "R" and match[2] in self.classes:
                above = self.inherited_specifiers(match[2])
                provided = None if None in (provided, above) else provided | above
            found = None if None in (found, provided) else found | provided
        self.specifiers[identity] = found
        return found

    def provided(self, name: str) -> frozenset[str] | None:
        """The field specifiers a decorator or base provides on its own: a
        provider's declared ones, none for a repository class or a plain name,
        and None for a name that does not resolve."""
        match = TOKEN.fullmatch(name)
        if name in PROVIDERS:
            return PROVIDERS[name]
        if match and match[1] == "R" and match[2] in self.providers:
            return self.providers[match[2]]
        if match and match[1] == "R" and match[2] in self.classes:
            return frozenset()
        return None if match else frozenset()

    def field_required(self, declared: DeclaredClass, field: ast.AnnAssign):
        """Whether a constructor requires a class-body field, or None when that
        is undecided."""
        identity = f"{declared.module}.{declared.qualname}"
        dictionary = self.typed_dictionary(identity)
        if dictionary is not False and field.value is None:
            keyword = next(
                (k.value for k in declared.node.keywords if k.arg == "total"), None
            )
            total = True if keyword is None else None
            if isinstance(keyword, ast.Constant) and isinstance(keyword.value, bool):
                total = keyword.value
            keyed = declared.types.required(field.annotation, total)
            return keyed if dictionary or keyed is True else None
        stores = initializer_stores(declared.node)
        if field.target.id in stores:
            return stores[field.target.id]
        assigned = field.value
        if assigned is None or (
            isinstance(assigned, ast.Constant) and assigned.value is Ellipsis
        ):
            return True
        if not isinstance(assigned, ast.Call):
            return False
        callee = declared.types.form(assigned.func)
        specifiers = self.constructor_specifiers(identity)
        if specifiers is None:
            return None if TOKEN.fullmatch(callee) else False
        return specifier_required(assigned) if callee in specifiers else False

    def shape(self, declared: DeclaredClass) -> None:
        identity = f"{declared.module}.{declared.qualname}"
        types, node = declared.types, declared.node
        fields = []
        for field in node.body:
            if not (
                isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
            ):
                continue
            if types.form(field.annotation) in (
                "typing.ClassVar",
                "dataclasses.KW_ONLY",
            ) or self.class_metadata(identity, field):
                continue
            required = self.field_required(declared, field)
            fields.append((field.target.id, field.annotation, required))
        added = types.method_annotations(node, {name for name, *_ in fields})
        try:
            typed = types.typed([*fields, *added])
        except Unreadable as error:
            if len(declared.names) >= MINIMUM_FIELDS:
                self.unreadable.append(
                    f"{declared.path}::{declared.qualname}:{node.lineno} {error}"
                )
            return
        if typed:
            self.typed[identity] = typed
        if len(typed) < MINIMUM_FIELDS:
            return
        bases = frozenset(
            match[2]
            for base in declared.bases
            if (match := TOKEN.fullmatch(base)) and match[1] == "R"
        )
        inherited = frozenset(
            name
            for ancestor in self.ancestry(identity)
            for name in self.classes[ancestor].names
        )
        protocol = any(base in PROTOCOLS for base in declared.bases)
        self.shapes.append(
            ShapedClass(
                declared.path,
                declared.module,
                declared.qualname,
                node,
                protocol,
                bases,
                inherited,
                typed,
            )
        )

    def owners(self, identity: str, name: str) -> frozenset[str]:
        """The repository classes at the root of the class's ancestry that
        declare the field."""
        if (identity, name) in self.owned:
            return self.owned[identity, name]
        self.owned[identity, name] = frozenset({identity})
        roots: set[str] = set()
        for base in self.classes[identity].bases:
            match = TOKEN.fullmatch(base)
            if match and match[1] == "R" and match[2] in self.classes:
                if name in self.classes[match[2]].names or any(
                    name in self.classes[a].names for a in self.ancestry(match[2])
                ):
                    roots |= self.owners(match[2], name)
        self.owned[identity, name] = frozenset(roots or {identity})
        return self.owned[identity, name]
