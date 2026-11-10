"""The classes a project's declared dependencies export, resolved from the
project's installed environment.

pyproject.toml [project] declares the dependencies and optional dependencies.
A declaration that is not a PEP 508 requirement, or whose marker does not
evaluate, stops the check naming pyproject.toml and its line. The environment's
interpreter reports its marker environment and site directories; every
declared distribution whose marker holds there must be installed in them.

A distribution's sources are the Python files its RECORD lists; an editable
install's (direct_url.json dir_info editable) are the importable Python files
under the existing directories its .pth files add to the import path; a .pth
line that starts with import runs code instead. An editable install that loads
its sources through an import hook, or whose path entries hold no Python file,
and a RECORD that lists no Python file, leave the distribution's classes
unknown: each is reported as
incomplete coverage, as is an exported class whose shape is unknown. A
distribution exports the module-level classes that its public modules bind to
public names, read from its sources.
"""

import ast
import json
import sys
import tomllib
from importlib.metadata import Distribution, distributions
from pathlib import Path
from typing import NoReturn

from packaging.markers import (
    InvalidMarker,
    UndefinedComparison,
    UndefinedEnvironmentName,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field, ValidationError

from code_health import ERROR_EXIT, run
from code_health.bindings import own_nodes
from code_health.imports import Modules
from code_health.python_metrics import python_trees
from code_health.shapes import ClassIndex

ENVIRONMENT = """
import json, os, platform, sys, sysconfig
version = sys.implementation.version
implementation = f"{version.major}.{version.minor}.{version.micro}"
if version.releaselevel != "final":
    implementation += version.releaselevel[0] + str(version.serial)
markers = {
    "implementation_name": sys.implementation.name,
    "implementation_version": implementation,
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
}
sites = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
print(json.dumps({"markers": markers, "sites": sites}))
"""


class Project(BaseModel, strict=True):
    """The pyproject.toml [project] keys that declare dependencies; an absent
    list declares none (PEP 621)."""

    name: str
    dependencies: list[str] = []
    optional_dependencies: dict[str, list[str]] = Field(
        default={}, alias="optional-dependencies"
    )
    dynamic: list[str] = []


class PyProject(BaseModel, strict=True):
    project: Project


class Environment(BaseModel, extra="forbid", strict=True):
    markers: dict[str, str]
    sites: list[str]


class InstalledDirectory(BaseModel):
    editable: bool = False


class DirectUrl(BaseModel):
    """The PEP 610 record of a distribution installed from a URL or directory."""

    url: str
    directory: InstalledDirectory | None = Field(default=None, alias="dir_info")


def unresolved(problem: str) -> NoReturn:
    """The dependencies cannot be resolved, so the check cannot run."""
    print(f"code-health: {problem}", file=sys.stderr)
    sys.exit(ERROR_EXIT)


def invalid(source: str, error: ValidationError) -> NoReturn:
    unresolved(
        "; ".join(
            f"{source}.{'.'.join(map(str, problem['loc']))}: {problem['msg']}"
            for problem in error.errors(include_input=False, include_url=False)
        )
    )


def declared(root: Path) -> tuple[str, list[tuple[Requirement, str, int]]]:
    """The project's name, and each declared requirement with the extra that
    declares it, empty for a base dependency, and its pyproject.toml line."""
    text = (root / "pyproject.toml").read_text()
    try:
        project = PyProject.model_validate(tomllib.loads(text)).project
    except tomllib.TOMLDecodeError as error:
        unresolved(f"pyproject.toml: {error}")
    except ValidationError as error:
        invalid("pyproject.toml", error)
    for key in ("dependencies", "optional-dependencies"):
        if key in project.dynamic:
            unresolved(f"pyproject.toml: [project].{key} is dynamic")
    declarations = [(entry, "") for entry in project.dependencies]
    declarations += [
        (entry, extra)
        for extra, entries in project.optional_dependencies.items()
        for entry in entries
    ]
    lines = text.splitlines()
    requirements = []
    for entry, extra in declarations:
        line = next(
            number
            for number, written in enumerate(lines, 1)
            if json.dumps(entry) in written
            or f"'{entry}'" in written
            or entry in written
        )
        try:
            requirements.append((Requirement(entry), extra, line))
        except InvalidRequirement as error:
            unresolved(f"pyproject.toml:{line}: {error}")
    return project.name, requirements


def importable(relative: Path) -> bool:
    """A file under an import path entry that an import can reach: every
    directory on the way is a valid identifier."""
    return all(part.isidentifier() for part in relative.parts[:-1])


def distribution_sources(
    distribution: Distribution,
) -> tuple[list[tuple[Path, list[str]]], str]:
    """Each directory a distribution's sources lie under with their paths
    there, or why the distribution's classes cannot be read."""
    name = distribution.metadata["Name"]
    direct = distribution.read_text("direct_url.json")
    editable = False
    if direct is not None:
        try:
            installed = DirectUrl.model_validate_json(direct)
        except ValidationError as error:
            invalid(f"{name} direct_url.json", error)
        editable = installed.directory is not None and installed.directory.editable
    if not editable:
        listed = [
            str(file)
            for file in distribution.files
            if file.suffix in (".py", ".pyi") and file.parts[0] != ".."
        ]
        if not listed:
            return [], f"{name}: its RECORD lists no Python source"
        return [(Path(distribution.locate_file("")), listed)], ""
    site = Path(distribution.locate_file(""))
    entries = [
        line.strip()
        for file in distribution.files
        if file.suffix == ".pth"
        for line in Path(distribution.locate_file(file)).read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    hooks = [entry for entry in entries if entry.startswith(("import ", "import\t"))]
    paths = [entry for entry in entries if entry not in hooks]
    directories = [site / entry for entry in paths if (site / entry).is_dir()]
    unread = [
        *(f"the import hook line {entry!r}" for entry in hooks),
        *(
            f"the missing path {entry!r}"
            for entry in paths
            if not (site / entry).is_dir()
        ),
    ]
    missing = (
        f"{name}: its editable install .pth names {', '.join(unread)}, "
        "whose sources are not read"
        if unread
        else ""
    )
    if not directories:
        return [], missing or f"{name}: its editable install names no source path"
    found = [
        (
            directory,
            [
                str(relative)
                for path in sorted(directory.rglob("*.py*"))
                if path.suffix in (".py", ".pyi")
                and importable(relative := path.relative_to(directory))
            ],
        )
        for directory in directories
    ]
    if not any(listed for _, listed in found):
        return [], missing or f"{name}: its editable path entries hold no Python source"
    return found, missing


def dependency_trees(
    root: Path, python: str
) -> tuple[dict[str, ast.Module], list[str]]:
    """The Python sources of every declared distribution that applies to the
    environment, keyed by their path under their import path entry, and the
    distributions whose sources cannot be read."""
    reported = run([python, "-c", ENVIRONMENT], root)
    try:
        environment = Environment.model_validate_json(reported)
    except ValidationError as error:
        invalid(f"{python} environment", error)
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in distributions(path=environment.sites)
    }
    name, requirements = declared(root)
    trees: dict[str, ast.Module] = {}
    incomplete = []
    for requirement, extra, line in requirements:
        if canonicalize_name(requirement.name) == canonicalize_name(name):
            continue
        markers = {**environment.markers, "extra": extra}
        try:
            applies = requirement.marker is None or requirement.marker.evaluate(markers)
        except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName) as error:
            unresolved(f"pyproject.toml:{line}: {requirement}: {error}")
        if not applies:
            continue
        distribution = installed.get(canonicalize_name(requirement.name))
        if distribution is None:
            unresolved(
                f"pyproject.toml:{line}: {requirement.name} is not installed in "
                f"the environment of {python}"
            )
        if distribution.files is None:
            unresolved(
                f"{requirement.name} in the environment of {python} "
                "has no RECORD of its files"
            )
        directories, missing = distribution_sources(distribution)
        if missing:
            incomplete.append(f"incomplete: {missing}")
        for directory, listed in directories:
            trees |= python_trees(directory, listed)
    return trees, incomplete


def public_names(modules: Modules, path: str, tree: ast.Module):
    """Each public name a module declares at its top level, with the file that
    defines it. Imports under an if TYPE_CHECKING block count: a package that loads
    its exports lazily declares them there."""
    for node in own_nodes(tree):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            yield path, node.name
        elif isinstance(node, ast.ImportFrom):
            imported = modules.imported(path, node)
            for (module, name), alias in zip(imported, node.names, strict=True):
                if module and name and not (alias.asname or name).startswith("_"):
                    yield modules.defining(module, name), name


def exported_shapes(
    trees: dict[str, ast.Module],
) -> tuple[ClassIndex, set[str], list[str]]:
    """The dependency's class index, the identities of the classes that public
    modules bind to public names and of the classes nested in them, and the
    classes whose shape is unknown. Each file lies under an import path entry,
    its only source root."""
    modules = Modules(trees, {""})
    public = {
        found
        for path, tree in trees.items()
        if not any(
            part.startswith("_") and part not in ("__init__.py", "__init__.pyi")
            for part in path.split("/")
        )
        for found in public_names(modules, path, tree)
    }
    index = ClassIndex(modules)
    exported = {
        shaped.identity
        for shaped in index.shapes
        if (shaped.path, shaped.qualname.split(".", 1)[0]) in public
        and "<locals>" not in shaped.qualname
    }
    return index, exported, [f"incomplete: {entry}" for entry in index.unreadable]
