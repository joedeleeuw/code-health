"""Project declarations: the import roots a repository's packaging and test
configuration declare, and the third-party copies it declares vendored.

A directory declares a project when it holds a pyproject.toml with a
[build-system], [project] or [tool.poetry] table, a setup.cfg with an
[options] or [metadata] section, or a setup.py that calls setup(). The
project's source roots are the directories its build backend imports packages
from: setuptools package-dir and packages.find where (pyproject.toml, setup.cfg
or a literal setup() keyword), hatch packages and sources, poetry packages
from, and maturin python-source. A project that declares none of these uses its
backend's default: its src directory when that holds Python files, the project
directory otherwise. A pytest pythonpath entry (pytest.ini, pyproject.toml,
tox.ini, setup.cfg, pytest.toml or .pytest.toml) is a source root of the test
run. A directory Python
itself puts first on the import path is a source root too: the directory of a
script that imports a package sitting beside it, and the base directory of a
conftest.py, the nearest directory above it outside every package, which
pytest prepends before loading it. Nothing else makes the checkout root a
source root.

The nearest pytest configuration above a module, in pytest's order of
precedence within a directory (pytest.toml, .pytest.toml, pytest.ini,
.pytest.ini, pyproject.toml's native [tool.pytest] table or its ini_options
table, tox.ini, setup.cfg), declares which of its functions and classes pytest
collects (python_files, python_classes and python_functions, with pytest's
defaults where unset) and the plugins its addopts load or block with -p. A
string option splits as a shell command line does; a list holds one argument
per element, and an element may carry -p together with its operand.
pyproject.toml declaring both pytest tables stops the check, as pytest
refuses it.

A pyproject.toml's [project] table names the entry points the project
installs: scripts, gui-scripts and each entry-points group map a name to a
module and, after a colon, the object the command or plugin loads from it.

Type checkers receive the repository files below the files and directories
that mypy's files option (pyproject.toml, mypy.ini, .mypy.ini or setup.cfg),
pyright's and basedpyright's include, pyrefly's project-includes, and the path
arguments of a type checker command in a tox.ini commands line name, relative
to the configuration's directory, where "." names that whole directory. A
file the same table excludes is not received: mypy's exclude regular
expressions, pyright's and basedpyright's exclude globs (pyright's defaults
**/node_modules, **/__pycache__ and **/.* when unset) and pyrefly's
project-excludes globs, each matched against the file's path relative to the
configuration's directory or against a directory above it.

A vendoring destination is the destination of a [tool.vendoring] table, or a
directory holding the vendoring tool's manifest, vendor.txt, that pins a
distribution whose package or module sits in that directory. A Python file
below a destination is a vendored copy, except the destination's own package
initializer and the files the table protects. The repository root holds the
repository's own packages, so a manifest there declares no copy.

A declaration file that does not decode stops the check, naming the file and,
for vendor.txt, the line.
"""

import ast
import configparser
import fnmatch
import os
import posixpath
import re
import shlex
import sys
import tomllib
from pathlib import Path
from typing import Any, NoReturn

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from pydantic import BaseModel, Field, ValidationError

from code_health import ERROR_EXIT
from code_health.imports import script_roots

INITIALIZERS = ("__init__.py", "__init__.pyi")
COMMENT = re.compile(r"(?:^|\s)#.*$")
OPTIONS = re.compile(r"\s+--?\w[\w-]*(?:[= ]\S+)?")
URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[a-z]+\+[a-z]+:)", re.IGNORECASE)
EGG = re.compile(r"#egg=([A-Za-z0-9][A-Za-z0-9._-]*)")
FINDERS = ("find_packages", "find_namespace_packages")
PYRIGHT_EXCLUDES = ["**/node_modules", "**/__pycache__", "**/.*"]
GLOB_TOKENS = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
CHECKER_COMMANDS = (
    "mypy",
    "pyright",
    "basedpyright",
    "pyrefly",
    "pytype",
    "pyre",
    "ty",
)


class FindPackages(BaseModel):
    where: list[str] = ["."]


class FoundPackages(BaseModel):
    find: FindPackages = FindPackages()


class Setuptools(BaseModel):
    package_dir: dict[str, str] = Field(default={}, alias="package-dir")
    packages: list[str] | FoundPackages = []


class HatchSources(BaseModel):
    packages: list[str] = []
    sources: list[str] | dict[str, str] = []


class HatchTargets(BaseModel):
    wheel: HatchSources = HatchSources()


class HatchBuild(HatchSources):
    targets: HatchTargets = HatchTargets()


class Hatch(BaseModel):
    build: HatchBuild = HatchBuild()


class PoetryPackage(BaseModel):
    source: str = Field(default=".", alias="from")


class Poetry(BaseModel):
    packages: list[PoetryPackage] = []


class Maturin(BaseModel):
    python_source: str = Field(default="", alias="python-source")


class PytestOptions(BaseModel):
    pythonpath: list[str] | str = []
    testpaths: list[str] | str = []
    python_files: list[str] | str = "test_*.py *_test.py"
    python_classes: list[str] | str = "Test"
    python_functions: list[str] | str = "test"
    addopts: list[str] | str = []


class Collection(BaseModel):
    """The names pytest collects, as glob patterns or name prefixes, and the
    plugins its options load or block."""

    folder: str
    paths: list[str]
    files: list[str]
    classes: list[str]
    functions: list[str]
    plugins: list[str]
    disabled: list[str]

    def collects_tree(self, path: str) -> bool:
        """The file lies in a tree pytest collects by default: under one of the
        configuration's testpaths, a literal folder or a glob over folders, or
        anywhere when it declares none or names its own folder."""
        if not self.paths:
            return True
        folders = [path, *parents(path)]
        for declared in self.paths:
            root = normal(posixpath.join(self.folder, declared))
            if root == "":
                return True
            if any(mark in root for mark in "*?["):
                if any(fnmatch.fnmatchcase(folder, root) for folder in folders):
                    return True
            elif path == root or path.startswith(f"{root}/"):
                return True
        return False

    def collects_file(self, path: str) -> bool:
        """A python_files glob matches the file's name, or its path when the
        glob holds a directory separator."""
        return any(
            fnmatch.fnmatchcase(f"/{path}", f"*/{pattern}")
            if "/" in pattern
            else fnmatch.fnmatchcase(posixpath.basename(path), pattern)
            for pattern in self.files
        )

    @staticmethod
    def matches(patterns: list[str], name: str) -> bool:
        return any(
            fnmatch.fnmatchcase(name, pattern)
            if any(mark in pattern for mark in "*?[")
            else name.startswith(pattern)
            for pattern in patterns
        )


class Pytest(PytestOptions, extra="allow"):
    """pyproject.toml's [tool.pytest]: pytest 9's native options, or the
    ini_options table."""

    ini_options: PytestOptions | None = None

    def options(self) -> PytestOptions:
        """The options pytest reads; it refuses native options beside the
        ini_options table."""
        if self.ini_options is None:
            return self
        if (self.model_fields_set | set(self.model_extra or {})) - {"ini_options"}:
            raise Undecodable("[tool.pytest] beside [tool.pytest.ini_options]")
        return self.ini_options


class PytestFile(BaseModel):
    pytest: PytestOptions = PytestOptions()


class Vendoring(BaseModel):
    destination: str
    protected_files: list[str] = Field(default=[], alias="protected-files")


class CheckedFiles(BaseModel):
    files: list[str] | str = []
    exclude: list[str] | str = []


class CheckedIncludes(BaseModel):
    include: list[str] = []
    exclude: list[str] = PYRIGHT_EXCLUDES


class CheckedProject(BaseModel):
    project_includes: list[str] = Field(default=[], alias="project-includes")
    project_excludes: list[str] = Field(default=[], alias="project-excludes")


class Tool(BaseModel):
    setuptools: Setuptools = Setuptools()
    hatch: Hatch = Hatch()
    poetry: Poetry | None = None
    maturin: Maturin = Maturin()
    pytest: Pytest | None = None
    vendoring: Vendoring | None = None
    mypy: CheckedFiles = CheckedFiles()
    pyright: CheckedIncludes = CheckedIncludes()
    basedpyright: CheckedIncludes = CheckedIncludes()
    pyrefly: CheckedProject = CheckedProject()


class ProjectTable(BaseModel):
    """pyproject.toml's [project]: the tables that name entry points, each a
    module and, after a colon, the object in it that the installed command or
    plugin loads."""

    scripts: dict[str, str] = {}
    gui_scripts: dict[str, str] = Field(default={}, alias="gui-scripts")
    entry_points: dict[str, dict[str, str]] = Field(default={}, alias="entry-points")

    def targets(self) -> set[tuple[str, str]]:
        """The module and the path from it to the object each entry point
        loads; one that names a module alone loads no object."""
        found = set()
        groups = [self.scripts, self.gui_scripts, *self.entry_points.values()]
        for target in (target for group in groups for target in group.values()):
            module, _, loaded = target.partition(":")
            named = loaded.split("[")[0].strip()
            if named:
                found.add((module.strip(), named))
        return found


class PyProject(BaseModel):
    build_system: dict[str, Any] | None = Field(default=None, alias="build-system")
    project: ProjectTable | None = None
    tool: Tool = Tool()


class Undecodable(Exception):
    """A declaration the decoders cannot read; the reader of the file names
    it."""


def linked_aliases(root: Path, trees) -> dict[str, str]:
    """Each path an import finds for a parsed module through a symlink to a
    directory inside the repository, with the module's own path."""
    aliased: dict[str, str] = {}
    for folder, subfolders, _ in os.walk(root):
        subfolders[:] = [name for name in subfolders if name != ".git"]
        for name in subfolders:
            link = Path(folder, name)
            if not link.is_symlink():
                continue
            try:
                target = link.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
            prefix = link.relative_to(root).as_posix()
            aliased.update(
                {
                    f"{prefix}/{module[len(target) + 1 :]}": module
                    for module in trees
                    if module.startswith(f"{target}/")
                }
            )
    return aliased


def undecodable(source: str, problem: object) -> NoReturn:
    print(f"code-health: {source}: {problem}", file=sys.stderr)
    sys.exit(ERROR_EXIT)


def normal(path: str) -> str:
    """A repository-relative directory, the root as the empty path."""
    collapsed = posixpath.normpath(path)
    return "" if collapsed == "." else collapsed


def separated(text: str) -> list[str]:
    return [part for part in re.split(r"[\s,]+", text) if part]


def arguments(option: list[str] | str) -> list[str]:
    """A pytest option's arguments: a string split as a shell command line, a
    list one argument per element."""
    if isinstance(option, list):
        return option
    try:
        return shlex.split(option)
    except ValueError as error:
        raise Undecodable(error) from error


def plugin_options(words: list[str]) -> list[str]:
    """The operand of each -p option: the next argument, or the rest of an
    argument that begins with -p."""
    return [
        (following if word == "-p" else word[2:]).strip()
        for word, following in zip(words, [*words[1:], ""][: len(words)], strict=True)
        if word.startswith("-p")
    ]


def glob_expression(pattern: str) -> str:
    """A pyright or pyrefly exclude glob as a regular expression over paths
    relative to the configuration's directory: ** spans directories, * and ?
    stay within one."""
    pattern = pattern.removeprefix("./").strip("/")
    tokens = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    translated = "".join(GLOB_TOKENS.get(token, re.escape(token)) for token in tokens)
    return rf"\A{translated}/?\Z"


def exclusions(expressions: list[str]) -> list[re.Pattern]:
    try:
        return [re.compile(expression) for expression in expressions]
    except re.error as error:
        raise Undecodable(error) from error


def parents(path: str) -> list[str]:
    """The directories above a repository path, nearest first, the repository
    root excluded."""
    found = []
    folder = posixpath.dirname(path)
    while folder:
        found.append(folder)
        folder = posixpath.dirname(folder)
    return found


def ini_options(folder: str, name: str, table: list) -> PytestOptions:
    """A pytest ini section's options, decoded at the file that holds them."""
    try:
        return PytestOptions.model_validate(dict(table))
    except ValidationError as error:
        undecodable(posixpath.join(folder, name), error)


def checker_tables(tool: Tool) -> list[tuple[list[str], list]]:
    """The files and directories each type checker's pyproject.toml table
    names, with the exclusions the same table declares."""
    files, mypy_excludes = tool.mypy.files, tool.mypy.exclude
    return [
        (
            separated(files) if isinstance(files, str) else files,
            exclusions(
                [mypy_excludes] if isinstance(mypy_excludes, str) else mypy_excludes,
            ),
        ),
        *(
            (
                table.include,
                exclusions([glob_expression(glob) for glob in table.exclude]),
            )
            for table in (tool.pyright, tool.basedpyright)
        ),
        (
            tool.pyrefly.project_includes,
            exclusions(
                [glob_expression(glob) for glob in tool.pyrefly.project_excludes],
            ),
        ),
    ]


def checker_arguments(words: list[str]) -> list[str]:
    """The paths a type checker command line names, after tox's factor
    conditions and its ignore-exit dash; nothing for any other command."""
    while words and (words[0].endswith(":") or words[0] == "-"):
        words = words[1:]
    if words[:2] in (["python", "-m"], ["python3", "-m"]):
        words = words[2:]
    if not words or words[0] not in CHECKER_COMMANDS:
        return []
    operands = words[2:] if words[1:2] == ["check"] else words[1:]
    return [
        word
        for word in operands
        if not word.startswith("-") and not any(mark in word for mark in "{}")
    ]


class Projects:
    """The repository's declared source roots and vendoring destinations, read
    from the declaration files in the directories above its Python files."""

    def __init__(self, root: Path, trees: dict[str, ast.Module]):
        self.root = root
        self.known = set(trees)
        self.source_roots: set[str] = set()
        self.destinations: dict[str, set[str]] = {}
        self.collections: dict[str, Collection] = {}
        self.checked: set[str] = set()
        self.checked_roots: set[str] = set()
        self.entry_points: set[tuple[str, str]] = set()
        self.aliases = linked_aliases(root, trees)
        self.source_roots |= script_roots(trees)
        folders = {parent for path in trees for parent in parents(path)}
        for folder in sorted({"", *folders}):
            explicit: set[str] = set()
            self.pytest_toml(folder)
            self.pytest_ini(folder)
            declared = self.pyproject(folder, explicit)
            self.tox_ini(folder)
            self.checker_configs(folder)
            declared |= self.setup_cfg(folder, explicit)
            setup = trees.get(posixpath.join(folder, "setup.py"))
            if setup is not None:
                declared |= self.setup_py(folder, setup, explicit)
            if explicit:
                self.source_roots |= explicit
            elif declared:
                self.source_roots.add(self.default_root(folder))
        for folder in sorted(folders - set(self.destinations)):
            if self.manifest_beside(folder):
                self.destinations[folder] = set()

    def default_root(self, folder: str) -> str:
        source = normal(posixpath.join(folder, "src"))
        holds = any(path.startswith(f"{source}/") for path in self.known)
        return source if holds else folder

    def pyproject(self, folder: str, explicit: set[str]) -> bool:
        source = posixpath.join(folder, "pyproject.toml")
        document = self.toml(source, PyProject)
        if document is None:
            return False
        tool = document.tool
        package_dir = tool.setuptools.package_dir
        explicit.update(self.package_roots(folder, package_dir))
        if isinstance(tool.setuptools.packages, FoundPackages):
            explicit.update(
                normal(posixpath.join(folder, where))
                for where in tool.setuptools.packages.find.where
            )
        for hatch in (tool.hatch.build, tool.hatch.build.targets.wheel):
            explicit.update(
                normal(posixpath.join(folder, posixpath.dirname(package.rstrip("/"))))
                for package in hatch.packages
            )
            prefixes = hatch.sources
            if isinstance(prefixes, dict):
                prefixes = [key for key, value in prefixes.items() if not value]
            explicit.update(
                normal(posixpath.join(folder, prefix)) for prefix in prefixes
            )
        if tool.poetry is not None:
            explicit.update(
                normal(posixpath.join(folder, package.source))
                for package in tool.poetry.packages
            )
        if tool.maturin.python_source:
            explicit.add(normal(posixpath.join(folder, tool.maturin.python_source)))
        self.tool_tables(source, tool)
        if document.project is not None:
            self.entry_points |= document.project.targets()
        return bool(
            document.build_system or document.project or tool.poetry is not None
        )

    def tool_tables(self, source: str, tool: Tool) -> None:
        """Record what a pyproject.toml's tool tables declare for the test
        runner, the type checkers and vendoring."""
        folder = posixpath.dirname(source)
        try:
            if tool.pytest is not None:
                self.collected(source, tool.pytest.options())
            for entries, patterns in checker_tables(tool):
                self.checked_entries(folder, entries, patterns)
        except Undecodable as problem:
            undecodable(source, problem)
        if tool.vendoring is not None:
            destination = normal(posixpath.join(folder, tool.vendoring.destination))
            self.destinations[destination] = set(tool.vendoring.protected_files)

    def checker_configs(self, folder: str) -> None:
        """Record the files mypy's ini configuration names and does not
        exclude."""
        for name in ("mypy.ini", ".mypy.ini", "setup.cfg"):
            source = posixpath.join(folder, name)
            parser = self.config(source)
            if parser is not None:
                files = parser.get("mypy", "files", fallback="")
                exclude = parser.get("mypy", "exclude", fallback="")
                try:
                    patterns = exclusions([exclude] if exclude else [])
                except Undecodable as problem:
                    undecodable(source, problem)
                self.checked_entries(folder, separated(files), patterns)

    def package_roots(self, folder: str, package_dir: dict[str, str]) -> set[str]:
        """The source roots a setuptools package_dir mapping declares: the
        directory mapped to the root package, and each package directory with
        its package path removed."""
        found = set()
        for package, directory in package_dir.items():
            relative = package.replace(".", "/")
            if not package:
                found.add(normal(posixpath.join(folder, directory)))
            elif directory.rstrip("/").endswith(relative):
                found.add(
                    normal(
                        posixpath.join(folder, directory.rstrip("/")[: -len(relative)])
                    )
                )
        return found

    def toml[Document: BaseModel](
        self, source: str, model: type[Document]
    ) -> Document | None:
        manifest = self.root / source
        if not manifest.is_file():
            return None
        try:
            return model.model_validate(tomllib.loads(manifest.read_text()))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError) as error:
            undecodable(source, error)

    def config(self, source: str) -> configparser.RawConfigParser | None:
        manifest = self.root / source
        if not manifest.is_file():
            return None
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(manifest.read_text(), source)
        except (configparser.Error, UnicodeDecodeError) as error:
            undecodable(source, error)
        return parser

    def setup_cfg(self, folder: str, explicit: set[str]) -> bool:
        parser = self.config(posixpath.join(folder, "setup.cfg"))
        if parser is None:
            return False
        package_dir = {}
        for line in parser.get("options", "package_dir", fallback="").splitlines():
            package, separator, directory = line.partition("=")
            if separator:
                package_dir[package.strip()] = directory.strip()
        explicit.update(self.package_roots(folder, package_dir))
        where = parser.get("options.packages.find", "where", fallback="")
        explicit.update(
            normal(posixpath.join(folder, entry)) for entry in separated(where)
        )
        if parser.has_section("tool:pytest"):
            self.collected(
                posixpath.join(folder, "setup.cfg"),
                ini_options(folder, "setup.cfg", parser.items("tool:pytest")),
            )
        return parser.has_section("options") or parser.has_section("metadata")

    def pytest_toml(self, folder: str) -> None:
        for name in ("pytest.toml", ".pytest.toml"):
            source = posixpath.join(folder, name)
            document = self.toml(source, PytestFile)
            if document is not None:
                self.collected(source, document.pytest)

    def pytest_ini(self, folder: str) -> None:
        for name in ("pytest.ini", ".pytest.ini"):
            parser = self.config(posixpath.join(folder, name))
            if parser is not None:
                options = parser.items("pytest") if parser.has_section("pytest") else []
                self.collected(
                    posixpath.join(folder, name), ini_options(folder, name, options)
                )

    def tox_ini(self, folder: str) -> None:
        parser = self.config(posixpath.join(folder, "tox.ini"))
        if parser is None:
            return
        if parser.has_section("pytest"):
            self.collected(
                posixpath.join(folder, "tox.ini"),
                ini_options(folder, "tox.ini", parser.items("pytest")),
            )
        for section in parser.sections():
            for line in parser.get(section, "commands", fallback="").splitlines():
                self.checked_entries(folder, checker_arguments(line.split()), [])

    def checked_entries(
        self, folder: str, entries: list[str], patterns: list[re.Pattern]
    ) -> None:
        """Record the repository files a type checker configuration names
        relative to its folder, as a file, a directory holding them or a glob,
        less those an exclusion matches as a path or, with a trailing slash,
        as a directory above it; and each file or directory it names as
        written."""
        for entry in entries:
            named = normal(posixpath.join(folder, entry))
            if any(mark in entry for mark in "*?["):
                covered = {p for p in self.known if fnmatch.fnmatchcase(p, named)}
            else:
                self.checked_roots.add(named)
                covered = {
                    path
                    for path in self.known
                    if not named or path == named or path.startswith(f"{named}/")
                }
            for path in covered:
                relative = posixpath.relpath(path, folder or ".")
                candidates = [relative, *(f"{above}/" for above in parents(relative))]
                if not any(
                    pattern.search(candidate)
                    for pattern in patterns
                    for candidate in candidates
                ):
                    self.checked.add(path)

    def collected(self, source: str, declared: PytestOptions) -> None:
        """Record a pytest configuration: its pythonpath roots, the names it
        collects and the plugins its addopts load or block with -p."""
        folder = posixpath.dirname(source)
        try:
            roots = arguments(declared.pythonpath)
            operands = plugin_options(arguments(declared.addopts))
            collection = Collection(
                folder=folder,
                paths=arguments(declared.testpaths),
                files=arguments(declared.python_files),
                classes=arguments(declared.python_classes),
                functions=arguments(declared.python_functions),
                plugins=[name for name in operands if not name.startswith("no:")],
                disabled=[name[3:] for name in operands if name.startswith("no:")],
            )
        except Undecodable as problem:
            undecodable(source, problem)
        self.source_roots.update(normal(posixpath.join(folder, root)) for root in roots)
        self.collections.setdefault(folder, collection)

    def setup_py(self, folder: str, tree: ast.Module, explicit: set[str]) -> bool:
        """Whether the setup.py calls setup(), and the roots its literal
        package_dir and find_packages arguments declare."""
        calls = False
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            callee = getattr(call.func, "id", getattr(call.func, "attr", ""))
            if callee == "setup":
                calls = True
                for keyword in call.keywords:
                    if keyword.arg == "package_dir" and isinstance(
                        keyword.value, ast.Dict
                    ):
                        package_dir = {
                            key.value: value.value
                            for key, value in zip(
                                keyword.value.keys, keyword.value.values, strict=True
                            )
                            if isinstance(key, ast.Constant)
                            and isinstance(value, ast.Constant)
                            and isinstance(key.value, str)
                            and isinstance(value.value, str)
                        }
                        explicit.update(self.package_roots(folder, package_dir))
            elif callee in FINDERS:
                where = [
                    *call.args[:1],
                    *(
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "where"
                    ),
                ]
                explicit.update(
                    normal(posixpath.join(folder, argument.value))
                    for argument in where
                    if isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                )
        return calls

    def manifest_beside(self, folder: str) -> bool:
        """The folder holds a vendor.txt pinning a distribution whose package or
        module is in the folder."""
        directory = self.root / folder
        manifest = directory / "vendor.txt"
        if not manifest.is_file():
            return False
        entries = {
            canonicalize_name(entry.name.removesuffix(entry.suffix))
            for entry in directory.iterdir()
            if entry.is_dir() or entry.suffix in (".py", ".pyi")
        }
        source = posixpath.join(folder, "vendor.txt")
        try:
            lines = manifest.read_bytes().decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            undecodable(source, error)
        pinned = set()
        for number, line in enumerate(lines, 1):
            try:
                pinned.add(canonicalize_name(pinned_name(line)))
            except InvalidRequirement as error:
                undecodable(f"{source}:{number}", error)
        return bool(pinned & entries)

    def vendored(self, path: str) -> bool:
        return any(
            posixpath.relpath(path, folder)
            not in (*INITIALIZERS, *self.destinations[folder])
            for folder in parents(path)
            if folder in self.destinations
        )


def pinned_name(line: str) -> str:
    """The distribution a pip requirements line names: a PEP 508 requirement,
    or a URL naming it with #egg=. Blank lines, comments, option lines and URLs
    without a name declare none; any other line that is not a requirement
    raises InvalidRequirement."""
    if URL.match(line.strip()):
        egg = EGG.search(line)
        return egg[1] if egg else ""
    text = OPTIONS.sub("", COMMENT.sub("", line)).strip()
    if not text or text.startswith("-"):
        return ""
    return Requirement(text).name
