"""Which Python files a repository distributes, read from its build configuration
with each supported backend's own discovery rules.

A distribution is a directory holding a setup.py, a setup.cfg with [metadata]
or [options], or a pyproject.toml with a [project] or [tool.poetry] table and
something that builds it: a [build-system] table, or one of those two files
beside it. A pyproject.toml nothing builds, the form uv reads as an
application it installs no package from, ships no module; its directory is
still where its modules import from.

- setuptools, also the backend of a build system that names none and of a
  pyproject.toml built by the setup.py or setup.cfg beside it: an
  explicit package list ships exactly those dotted packages; a find
  configuration (packages.find, find:, find_packages) ships every package under
  its where directories that its include and exclude patterns allow; with
  neither, src-layout ships every package under src/, and flat-layout ships the
  one top-level directory with a tracked file outside setuptools' reserved
  names, which setuptools takes for a package with or without an __init__.py.
- hatchling: wheel packages ship each listed directory tree; with none listed,
  the package named after the project, under src/ or the root. A wheel target
  that selects files instead (include, only-include, sources, force-include)
  is unresolved.
- poetry-core: each packages include ships its tree; with none listed, the
  package named after the project, under src/ or the root; package-mode false
  ships nothing.
- flit_core: the module named in [tool.flit.module], or the one named after the
  project; a dotted name ships that subpackage of a namespace package.
- briefcase: each application's sources ship their package trees.
- pbr, declared as the backend or through a setup.cfg [files] section: each
  package [files] or [options] lists with its subpackages, or the package
  named after [metadata] name; with a [project] table and no [files] packages,
  the setuptools configuration above, a listed package again shipping its
  subpackages.

Any other backend, a package list that is not literal, or a flat layout with
several such directories, a build setuptools refuses, is unresolved, as is a
pyproject.toml with a build system that declares no project or Briefcase
application, and one with none that uv is told to package ([tool.uv] package),
which uv builds with a backend of its own choosing: its candidates are
incomplete analysis and claim no contract. A configuration that does not
parse, or a project table without the name a backend's discovery needs, stops
the check naming the file.
A public module is a shipped module none of whose path components starts with
an underscore. Each distribution's base directory is also where its packages
import from.
"""

import ast
import configparser
import fnmatch
import posixpath
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from code_health import stop
from code_health.manifests import (
    Manifests,
    TomlManifest,
    called_name,
    config_table,
    separated,
    strings,
    text,
)

RESERVED = frozenset(
    "ci bin debian doc docs documentation manpages news newsfragments changelog"
    " test tests unit_test unit_tests example examples scripts tools util utils"
    " python build dist venv env requirements tasks fabfile site_scons benchmark"
    " benchmarks exercise exercises htmlcov".split()
)
BACKENDS = {
    "setuptools.build_meta": "setuptools",
    "setuptools.build_meta:__legacy__": "setuptools",
    "hatchling.build": "hatchling",
    "poetry.core.masonry.api": "poetry",
    "flit_core.buildapi": "flit",
    "pbr.build": "pbr",
}
FIND_PACKAGES_PARAMETERS = ("where", "exclude", "include")
HATCH_FILE_SELECTION = ("include", "only-include", "sources", "force-include")


@dataclass
class Distribution:
    """Exact dotted packages, whole trees under top-level names, a find over
    patterns, or unresolved."""

    base: str
    kind: str
    names: set[str] = field(default_factory=set)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    def package(self, path: str) -> str | None:
        """The dotted package a file sits in, relative to the base."""
        prefix = f"{self.base}/" if self.base else ""
        if not path.startswith(prefix):
            return None
        return ".".join(PurePosixPath(path[len(prefix) :]).parts[:-1])

    def ships(self, dotted: str) -> bool:
        if self.kind == "exact":
            return dotted in self.names
        if self.kind == "tree":
            return any(
                dotted == name or dotted.startswith(f"{name}.") for name in self.names
            )
        allowed = not self.include or any(
            fnmatch.fnmatch(dotted, p) for p in self.include
        )
        excluded = any(fnmatch.fnmatch(dotted, p) for p in self.exclude)
        return bool(dotted) and allowed and not excluded


class Distributions:
    """The repository's distributions, found next to its Python files."""

    def __init__(self, manifests: Manifests, tracked: Iterable[str]):
        folders = {""}
        for path in manifests.trees:
            parent = PurePosixPath(path).parent
            folders.update(str(p) for p in (parent, *parent.parents) if str(p) != ".")
        subdirectories: dict[str, set[str]] = defaultdict(set)
        for path in tracked:
            parts = PurePosixPath(path).parts[:-1]
            for depth, name in enumerate(parts):
                subdirectories["/".join(parts[:depth])].add(name)
        self.found: list[Distribution] = []
        for folder in sorted(folders):
            directory = BuildDirectory(manifests, folder, subdirectories[folder])
            self.found += directory.distributions()
        self.import_roots = sorted({distribution.base for distribution in self.found})

    def status(self, path: str) -> str:
        """ "public", "unshipped" or "unresolved" for one module."""
        unresolved = False
        for distribution in self.found:
            dotted = distribution.package(path)
            if dotted is None:
                continue
            if distribution.kind == "unresolved":
                unresolved = True
            elif distribution.ships(dotted):
                parts = PurePosixPath(path).with_suffix("").parts
                if any(p.startswith("_") and p != "__init__" for p in parts):
                    return "unshipped"
                return "public"
        return "unresolved" if unresolved else "unshipped"

    def shipping(self, path: str) -> bool | None:
        """Whether a distribution ships the module; None when only a
        distribution whose build configuration does not resolve covers it."""
        unresolved = False
        for distribution in self.found:
            dotted = distribution.package(path)
            if dotted is None:
                continue
            if distribution.kind == "unresolved":
                unresolved = True
            elif distribution.ships(dotted):
                return True
        return None if unresolved else False


def backend(project: TomlManifest) -> str | None:
    """The supported backend the build system declares; setuptools when it
    declares none, None for any other."""
    declared = project.setting("build-system", "build-backend")
    if declared is None:
        return "setuptools"
    return BACKENDS.get(text(project.path, "build-system.build-backend", declared))


def declares_project(project: TomlManifest) -> bool:
    return (
        project.setting("project") is not None
        or project.setting("tool", "poetry") is not None
    )


def package_name(project: TomlManifest) -> str:
    """The import name of the project: its distribution name normalized,
    which PEP 621 and poetry both require."""
    name = project.setting("project", "name")
    field_name = "project.name"
    if name is None:
        name = project.setting("tool", "poetry", "name")
        field_name = "tool.poetry.name"
    if name is None:
        stop(f"{project.path} declares no project name")
    return re.sub(r"[-.]+", "_", text(project.path, field_name, name)).lower()


def literal_strings(node: ast.expr) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    strings = [
        e.value
        for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    return strings if len(strings) == len(node.elts) else None


class BuildDirectory:
    """One directory of the repository, the directories directly under it
    that hold a tracked file, and the distributions its build configuration
    declares, with paths relative to the repository root."""

    def __init__(self, manifests: Manifests, folder: str, subdirectories: set[str]):
        self.manifests = manifests
        self.directory = manifests.root / folder
        self.folder = folder
        self.subdirectories = subdirectories

    def within(self, child: str) -> str:
        joined = str(PurePosixPath(self.folder or ".", child))
        return "" if joined == "." else joined

    def distributions(self) -> list[Distribution]:
        setup_cfg = self.directory / "setup.cfg"
        project = self.manifests.toml(posixpath.join(self.folder, "pyproject.toml"))
        declared_cfg = self.manifests.ini(posixpath.join(self.folder, "setup.cfg"))
        parser = (
            configparser.RawConfigParser() if declared_cfg is None else declared_cfg
        )
        setup_py = self.manifests.setup_py(self.folder)
        if project is not None and project.setting("tool", "briefcase", "app"):
            return self.from_briefcase(project)
        if project is not None and backend(project) == "pbr":
            if parser.has_option("files", "packages") or not declares_project(project):
                return self.from_pbr(setup_cfg, parser)
        configured = parser.has_section("metadata") or parser.has_section("options")
        packaged = configured or parser.has_option("files", "packages")
        buildable = packaged or setup_py is not None
        declared = project is not None and declares_project(project)
        if declared and (buildable or project.setting("build-system") is not None):
            return self.from_pyproject(project)
        if parser.has_option("files", "packages"):
            return self.from_pbr(setup_cfg, parser)
        if configured:
            return self.from_setup_cfg(parser)
        if setup_py is not None:
            return self.from_setup_py(setup_py)
        return self.unbuilt(project)

    def unbuilt(self, project: TomlManifest | None) -> list[Distribution]:
        """What a directory with no setup file and no built project holds:
        nothing without a pyproject.toml; an unresolved distribution where it
        has a build system, or uv is told to package it; otherwise one that
        ships no module, whose directory its modules still import from."""
        if project is None:
            return []
        packaged = project.setting("tool", "uv", "package") is True
        built = packaged or project.setting("build-system") is not None
        return [Distribution(self.folder, "unresolved" if built else "exact")]

    def from_pbr(self, setup_cfg: Path, parser: configparser.RawConfigParser):
        """pbr's own discovery: each package [files] or [options] lists with all
        its subpackages, or the package named after [metadata] name. With a
        [project] table and no [files] packages, pbr builds through setuptools'
        configuration instead."""
        for section in ("files", "options"):
            if parser.has_option(section, "packages"):
                packages = separated(parser.get(section, "packages"))
                return [Distribution(self.folder, "tree", set(packages))]
        if not parser.has_option("metadata", "name"):
            stop(f"{setup_cfg} names no package for pbr")
        name = re.sub(r"[-.]+", "_", parser.get("metadata", "name")).lower()
        return [Distribution(self.folder, "tree", {name})]

    def automatic(self) -> list[Distribution]:
        """setuptools discovery with nothing configured."""
        if (self.directory / "src").is_dir():
            return [Distribution(self.within("src"), "find")]
        tops = {
            name
            for name in self.subdirectories
            if name.isidentifier() and name not in RESERVED and name[0] != "_"
        }
        kind = "tree" if len(tops) == 1 else "unresolved"
        return [Distribution(self.folder, kind, tops)]

    def named(self, name: str) -> list[Distribution]:
        """The one package a backend ships under its dotted name, from src/ or
        the directory itself."""
        in_src = (self.directory / "src" / name.replace(".", "/")).exists()
        base = self.within("src") if in_src else self.folder
        return [Distribution(base, "tree", {name})]

    def from_pyproject(self, project: TomlManifest) -> list[Distribution]:
        declared = backend(project)
        if declared in ("setuptools", "pbr"):
            return self.from_setuptools_table(project)
        if declared == "hatchling":
            return self.from_hatch_tables(project)
        if declared == "poetry":
            if project.setting("tool", "poetry", "package-mode") is False:
                return []
            entries = project.setting("tool", "poetry", "packages")
            if entries is None:
                return self.named(package_name(project))
            return self.poetry_packages(project.path, entries)
        if declared == "flit":
            module = project.setting("tool", "flit", "module", "name")
            if module is None:
                return self.named(package_name(project))
            return self.named(text(project.path, "tool.flit.module.name", module))
        return [Distribution(self.folder, "unresolved")]

    def poetry_packages(self, config: Path, entries) -> list[Distribution]:
        """Each [tool.poetry] packages include ships its tree from its from
        directory."""
        field_name = "tool.poetry.packages"
        if not isinstance(entries, list):
            stop(f"{config}: {field_name} is not a list")
        found = []
        for index, entry in enumerate(entries):
            where = f"{field_name}[{index}]"
            listed = config_table(config, where, entry)
            if "include" not in listed:
                stop(f"{config}: {where}.include is missing")
            include = text(config, f"{where}.include", listed["include"])
            base = (
                self.within(text(config, f"{where}.from", listed["from"]))
                if "from" in listed
                else self.folder
            )
            found.append(Distribution(base, "tree", {include}))
        return found

    def from_briefcase(self, project: TomlManifest) -> list[Distribution]:
        """Briefcase ships each application's sources, a package tree each."""
        field_name = "tool.briefcase.app"
        apps = config_table(
            project.path, field_name, project.setting("tool", "briefcase", "app")
        )
        found = []
        for app_name, declared in apps.items():
            app = config_table(project.path, f"{field_name}.{app_name}", declared)
            if "sources" not in app:
                stop(f"{project.path}: {field_name}.{app_name}.sources is missing")
            sources = strings(
                project.path, f"{field_name}.{app_name}.sources", app["sources"]
            )
            found += [
                Distribution(
                    self.within(str(PurePosixPath(source).parent)),
                    "tree",
                    {PurePosixPath(source).name},
                )
                for source in sources
            ]
        return found

    def from_hatch_tables(self, project: TomlManifest) -> list[Distribution]:
        """[tool.hatch.build] and its wheel target: listed packages, the project's
        package when none are listed, unresolved when they select files."""
        declared = (
            ("tool.hatch.build", project.setting("tool", "hatch", "build")),
            (
                "tool.hatch.build.targets.wheel",
                project.setting("tool", "hatch", "build", "targets", "wheel"),
            ),
        )
        tables = [
            (field_name, config_table(project.path, field_name, settings))
            for field_name, settings in declared
            if settings is not None
        ]
        if any(
            key in settings for _, settings in tables for key in HATCH_FILE_SELECTION
        ):
            return [Distribution(self.folder, "unresolved")]
        packages = [
            package
            for field_name, settings in tables
            if "packages" in settings
            for package in strings(
                project.path, f"{field_name}.packages", settings["packages"]
            )
        ]
        if not packages:
            return self.named(package_name(project))
        return [
            Distribution(
                self.within(str(PurePosixPath(p).parent)),
                "tree",
                {PurePosixPath(p).name},
            )
            for p in packages
        ]

    def from_setuptools_table(self, project: TomlManifest) -> list[Distribution]:
        """[tool.setuptools]: an explicit package list under its package-dir,
        each with its subpackages under pbr, a packages.find table, or automatic
        discovery when it names neither."""
        path, field_name = project.path, "tool.setuptools.packages"
        package_dir = project.setting("tool", "setuptools", "package-dir", "")
        base = (
            self.folder
            if package_dir is None
            else self.within(text(path, "tool.setuptools.package-dir", package_dir))
        )
        packages = project.setting("tool", "setuptools", "packages")
        if isinstance(packages, list):
            kind = "tree" if backend(project) == "pbr" else "exact"
            return [Distribution(base, kind, set(strings(path, field_name, packages)))]
        if packages is None:
            return self.automatic()
        if "find" not in config_table(path, field_name, packages):
            return [Distribution(self.folder, "unresolved")]
        find = config_table(path, f"{field_name}.find", packages["find"])
        options = {
            option: strings(path, f"{field_name}.find.{option}", find[option])
            for option in ("where", "include", "exclude")
            if option in find
        }
        return [
            Distribution(
                self.within(folder),
                "find",
                set(),
                options["include"] if "include" in options else [],
                options["exclude"] if "exclude" in options else [],
            )
            for folder in (options["where"] if "where" in options else ["."])
        ]

    def from_setup_cfg(self, parser: configparser.RawConfigParser):
        if not parser.has_option("options", "packages"):
            return self.automatic()
        packages = parser.get("options", "packages").strip()
        if not packages.startswith("find"):
            return [Distribution(self.folder, "exact", set(separated(packages)))]
        section = "options.packages.find"

        def option_values(option: str) -> list[str]:
            if not parser.has_option(section, option):
                return []
            return separated(parser.get(section, option))

        where = option_values("where")
        return [
            Distribution(
                self.within(where[0]) if where else self.folder,
                "find",
                set(),
                option_values("include"),
                option_values("exclude"),
            )
        ]

    def from_setup_py(self, tree: ast.Module) -> list[Distribution]:
        """setup(packages=[...]) ships those packages; find_packages(where,
        exclude, include) finds them; no packages argument uses discovery; any
        other expression is unresolved."""
        keyword = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.keyword) and n.arg == "packages"
            ),
            None,
        )
        if keyword is None:
            return self.automatic()
        exact = literal_strings(keyword.value)
        if exact is not None:
            return [Distribution(self.folder, "exact", set(exact))]
        call = keyword.value
        if not (isinstance(call, ast.Call) and called_name(call) == "find_packages"):
            return [Distribution(self.folder, "unresolved")]
        options = dict(zip(FIND_PACKAGES_PARAMETERS, call.args, strict=False))
        options |= {k.arg: k.value for k in call.keywords}
        where = options.pop("where", None)
        if isinstance(where, ast.Constant) and not isinstance(where.value, str):
            stop(f"{self.directory / 'setup.py'}: find_packages.where is not a string")
        patterns = {name: literal_strings(value) for name, value in options.items()}
        if (where is not None and not isinstance(where, ast.Constant)) or None in (
            patterns.values()
        ):
            return [Distribution(self.folder, "unresolved")]
        return [
            Distribution(
                self.folder if where is None else self.within(where.value),
                "find",
                set(),
                patterns["include"] if "include" in patterns else [],
                patterns["exclude"] if "exclude" in patterns else [],
            )
        ]
