"""What a class body declares: the class-level names that are fields, the
attributes its initializer stores with their requiredness, the attributes its
methods read from and store on the instance, its field specifier calls, and its
resolved bases and decorators. dataclasses.KW_ONLY and ClassVar are markers,
not fields; a store whose value reads the same attribute, or an augmented
assignment, rewrites a field rather than declaring one.
"""

import ast
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_health.shapes import FieldTypes

DEFAULTS = ("default", "default_factory", "factory")


def specifier_required(call: ast.Call) -> bool | None:
    """Whether a field specifier call leaves its field required: not with
    init=False, a default, default_factory or factory, or a positional default
    other than Ellipsis; undecided when a keyword is unpacked or init is not a
    literal."""
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if None in keywords:
        return None
    init = keywords.get("init")
    if init is not None and not isinstance(init, ast.Constant):
        return None
    if init is not None and init.value is False:
        return False
    given = [keywords[name] for name in DEFAULTS if name in keywords]
    given += call.args[:1]
    return all(
        isinstance(default, ast.Constant) and default.value is Ellipsis
        for default in given
    )


def initializer_stores(node: ast.ClassDef) -> dict[str, bool]:
    """The attributes the class's own __init__ stores on its receiver from one
    of its parameters unchanged, and whether the constructor requires that
    parameter: positional or keyword-only, without a default."""
    method = next(
        (
            member
            for member in node.body
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name == "__init__"
        ),
        None,
    )
    if method is None:
        return {}
    arguments = method.args
    positional = [*arguments.posonlyargs, *arguments.args]
    if not positional:
        return {}
    receiver, first_default = (
        positional[0].arg,
        len(positional) - len(arguments.defaults),
    )
    required = {
        argument.arg: index < first_default
        for index, argument in enumerate(positional[1:], start=1)
    }
    required |= {
        argument.arg: default is None
        for argument, default in zip(
            arguments.kwonlyargs, arguments.kw_defaults, strict=True
        )
    }
    rebound = {
        name.id
        for name in ast.walk(method)
        if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)
    }
    stores: dict[str, bool] = {}
    for statement in ast.walk(method):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = getattr(statement, "targets", None) or [statement.target]
        source = statement.value
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == receiver
                and isinstance(source, ast.Name)
                and source.id in required
                and source.id not in rebound
            ):
                stores.setdefault(target.attr, required[source.id])
    return stores


def receivers(method: ast.AST) -> set[str]:
    """The name a method's receiver parameter binds."""
    return {argument.arg for argument in method.args.args[:1]}


def received(attribute: ast.Attribute, receivers: set[str]) -> bool:
    """Whether the attribute is read from or stored on self, cls, type(self)
    or self.__class__."""
    owner = attribute.value
    if isinstance(owner, ast.Call) and getattr(owner.func, "id", "") == "type":
        owner = owner.args[0] if owner.args else owner
    if isinstance(owner, ast.Attribute) and owner.attr == "__class__":
        owner = owner.value
    return isinstance(owner, ast.Name) and owner.id in receivers


def instance_reads(node: ast.ClassDef) -> set[str]:
    """The attributes the class's methods load from the receiver."""
    return {
        attribute.attr
        for method in node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        for attribute in ast.walk(method)
        if isinstance(attribute, ast.Attribute)
        and isinstance(attribute.ctx, ast.Load)
        and received(attribute, receivers(method))
    }


def instance_stores(node: ast.ClassDef) -> set[str]:
    """The attributes the class's methods store on the receiver as fields. An
    assignment whose value reads the same attribute of the receiver rewrites
    what the instance already holds and gives it no field."""
    found = set()
    for method in node.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        receiver = receivers(method)
        stored: list[str] = []
        rewritten: list[str] = []
        for statement in ast.walk(method):
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                read = {
                    attribute.attr
                    for attribute in ast.walk(statement.value or statement)
                    if isinstance(attribute, ast.Attribute)
                    and isinstance(attribute.ctx, ast.Load)
                    and received(attribute, receiver)
                }
                targets = getattr(statement, "targets", None) or [statement.target]
                rewritten += [
                    attribute.attr
                    for target in targets
                    for attribute in ast.walk(target)
                    if isinstance(attribute, ast.Attribute)
                    and received(attribute, receiver)
                    and (attribute.attr in read or isinstance(statement, ast.AugAssign))
                ]
            if (
                isinstance(statement, ast.Attribute)
                and isinstance(statement.ctx, ast.Store)
                and received(statement, receiver)
            ):
                stored.append(statement.attr)
        found |= {name for name in stored if stored.count(name) > rewritten.count(name)}
    return found


def declared_names(node: ast.ClassDef, types: "FieldTypes") -> frozenset[str]:
    """The field names the class declares: its class-body annotations, ClassVar
    and KW_ONLY aside, and the attributes its methods store on the instance."""
    annotated = {
        field.target.id
        for field in node.body
        if isinstance(field, ast.AnnAssign)
        and isinstance(field.target, ast.Name)
        and types.form(field.annotation)
        not in ("typing.ClassVar", "dataclasses.KW_ONLY")
    }
    return frozenset(annotated | instance_stores(node))


def class_level_names(node: ast.ClassDef) -> frozenset[str]:
    names = set()
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(
            statement.target, ast.Name
        ):
            names.add(statement.target.id)
        elif isinstance(statement, ast.Assign):
            names.update(
                target.id
                for target in statement.targets
                if isinstance(target, ast.Name)
            )
    return frozenset(names)


def resolved_heads(node: ast.ClassDef, types: "FieldTypes") -> tuple:
    """The class's resolved bases and decorators and the field names it
    declares; raises Unreadable when a string annotation or base is not an
    expression."""
    bases = [types.form(base) for base in node.bases] + [
        types.form(keyword.value)
        for keyword in node.keywords
        if keyword.arg == "metaclass"
    ]
    decorators = [
        types.form(decorator.func if isinstance(decorator, ast.Call) else decorator)
        for decorator in node.decorator_list
    ]
    return bases, decorators, declared_names(node, types)
