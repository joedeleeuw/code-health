"""What a name refers to, asked of one pyright language server per repository:
where the name at a position is declared, and which parameter of a call
receives an argument.

A declaration is in the repository when pyright places it in a measured file.
Any other is outside and is known by its module and name, the first word of
the range pyright gives, which for an annotated variable runs on through the
annotation. A name pyright cannot
place has no declaration. Nor has a name it cannot follow through a circular
import: it answers that with the import's own range under the file of the
module imported, so a location names nothing when it is the range where a
from-import of the asking file binds the name asked about and its own file
spells no name of that import there, the name a from-import binds in its own
file, or outside its file's text.

The server is given no workspace, so it reads exactly the files the checker
opens in it and what they import, and never the repository's own type checker
configuration; a script without a .py suffix is analysed like any module. It
resolves imports where the checker tells it to search: the repository's import
roots and the packages its environments install. An import it still cannot
resolve is reported, since every name read from one counts as used. Positions
are the AST's: one-based lines and UTF-8 byte columns.

Pyright does not analyse code under a condition it evaluates as false for the
platform and version it checks, or past a statement that never returns, and
places no name there. It marks each such range unnecessary, which is how the
checker knows one: an import there is not unresolved.
"""

import ast
import io
import itertools
import json
import re
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from urllib.parse import unquote, urlparse

from code_health import stop

SERVER = ["uvx", "--from", "pyright==1.1.407", "pyright-langserver", "--stdio"]
LINE_END = re.compile(r"\r\n|\r|\n")
WORD = re.compile(r"\w+")
UNNECESSARY = 1
PACKAGE_ROOTS = (
    ("/stdlib/", 0),
    ("/site-packages/", 0),
    ("/dist-packages/", 0),
    ("/stubs/", 1),
)


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    column: int


@dataclass(frozen=True)
class Search:
    """Where imports resolve: the interpreter that supplies the standard
    library, and the directories searched after it."""

    interpreter: str
    directories: list[str]


@dataclass(frozen=True)
class Declaration:
    """Where a name is declared: a site in a measured file, or the module and
    name of a declaration outside the repository."""

    site: Site | None
    module: str
    name: str


def expression_site(path: str, expression: ast.expr) -> Site | None:
    """The site of the last name an expression spells, through a call or a
    subscript; None for an expression that names nothing."""
    if isinstance(expression, ast.Call):
        return expression_site(path, expression.func)
    if isinstance(expression, ast.Subscript):
        return expression_site(path, expression.value)
    if isinstance(expression, ast.Name):
        return Site(path, expression.lineno, expression.col_offset)
    if isinstance(expression, ast.Attribute):
        column = expression.end_col_offset - len(expression.attr.encode())
        return Site(path, expression.end_lineno, column)
    return None


DOT = (tokenize.OP, ".")


def token_name_site(
    lines: list[str], lineno: int, col_offset: int, parts: list[str]
) -> tuple[int, int]:
    """The line and byte column of the last component of the first dotted name
    spelling parts, tokenized from a line and byte column."""
    first = lines[lineno - 1]
    start = len(first.encode()[:col_offset].decode())
    text = "\n".join([first, *lines[lineno:]])
    matched = 0
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.start[0] == 1 and tok.start[1] < start:
            continue
        spelled = (tok.type, tok.string)
        expected = (tokenize.NAME, parts[matched // 2]) if matched % 2 == 0 else DOT
        if spelled == expected:
            matched += 1
        else:
            matched = 1 if spelled == (tokenize.NAME, parts[0]) else 0
        if matched == 2 * len(parts) - 1:
            row = lineno - 1 + tok.start[0]
            return row, len(lines[row - 1][: tok.start[1]].encode())
    raise ValueError(f"line {lineno}: no token spells {'.'.join(parts)}")


def imported_modules(node: ast.AST, lines: list[str]) -> list[tuple[int, int, str]]:
    """The line, the byte column of the last component and the dotted name of
    each module an import statement names; a relative import names none."""
    if isinstance(node, ast.Import):
        return [
            (
                *token_name_site(
                    lines, alias.lineno, alias.col_offset, alias.name.split(".")
                ),
                alias.name,
            )
            for alias in node.names
        ]
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        named = token_name_site(
            lines, node.lineno, node.col_offset, node.module.split(".")
        )
        return [(*named, node.module)]
    return []


def bound_names(
    tree: ast.Module, lines: list[str]
) -> dict[tuple[int, int, int], set[str]]:
    """Per line and columns, as the server counts them, of each name and each
    alias a from-import spells: the names that import reads and binds."""
    found = {}
    for node in ast.walk(tree):
        for alias in node.names if isinstance(node, ast.ImportFrom) else []:
            names = {alias.name, alias.asname or alias.name}
            line = alias.lineno - 1
            start = utf16_column(lines[line], alias.col_offset)
            size = len(alias.name.encode("utf-16-le")) // 2
            found[line, start, start + size] = names
            if alias.asname:
                line = alias.end_lineno - 1
                end = utf16_column(lines[line], alias.end_col_offset)
                size = len(alias.asname.encode("utf-16-le")) // 2
                found[line, end - size, end] = names
    return found


def utf16_column(text: str, byte_column: int) -> int:
    prefix = text.encode()[:byte_column].decode()
    return len(prefix.encode("utf-16-le")) // 2


def byte_column(text: str, utf16_position: int) -> int:
    prefix = text.encode("utf-16-le")[: utf16_position * 2].decode("utf-16-le")
    return len(prefix.encode())


def outside_module(location: Path) -> str:
    """The dotted module a file outside the repository holds, read from where
    it sits under the standard library stubs or an installed distribution."""
    text = location.as_posix()
    parts = [location.name]
    for marker, skipped in PACKAGE_ROOTS:
        if marker in text:
            parts = text.rsplit(marker, 1)[1].split("/")[skipped:]
            break
    dotted = ".".join(parts).rsplit(".", 1)[0]
    return dotted.removesuffix(".__init__")


class Resolver:
    """One language server over the measured files of one repository."""

    def __init__(
        self,
        root: Path,
        sources: dict[str, str],
        trees: dict[str, ast.Module],
        search: Search,
    ):
        """sources holds the text and trees the syntax tree of each measured
        file."""
        self.root = root.resolve()
        self.searched = [Path(directory) for directory in search.directories]
        self.settings = {
            "python": {"pythonPath": search.interpreter},
            "python.analysis": {
                "extraPaths": search.directories,
                "autoSearchPaths": False,
                "diagnosticMode": "openFilesOnly",
            },
        }
        self.measured = set(sources)
        self.lines: dict[str, list[str]] = {}
        self.imported: dict[str, dict[tuple[int, int, int], set[str]]] = {}
        self.outside_lines: dict[Path, list[str]] = {}
        self.declared: dict[Site, list[Declaration]] = {}
        self.types: dict[Site, str] = {}
        self.skipped: dict[str, list[tuple[tuple[int, int], tuple[int, int]]]] = {}
        self.request_ids = itertools.count(1)
        self.errors = TemporaryFile()
        try:
            self.server = subprocess.Popen(
                SERVER,
                cwd=self.root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
            )
        except OSError:
            self.errors.close()
            raise
        try:
            self.request(
                "initialize",
                {
                    "processId": None,
                    "rootUri": None,
                    "capabilities": {
                        "workspace": {"configuration": True},
                        "textDocument": {
                            "diagnostic": {},
                            "publishDiagnostics": {
                                "tagSupport": {"valueSet": [UNNECESSARY]}
                            },
                        },
                    },
                },
            )
            self.notify("initialized", {})
            for path, text in sources.items():
                self.open(path, text, trees[path])
        except BaseException:
            self.close()
            raise

    def open(self, path: str, text: str, tree: ast.Module) -> None:
        """Hand the server a file of the checkout, so positions in it resolve."""
        self.lines[path] = LINE_END.split(text)
        self.imported[path] = bound_names(tree, self.lines[path])
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": self.uri(path),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def __enter__(self) -> "Resolver":
        return self

    def __exit__(self, *raised) -> None:
        self.close()

    def close(self) -> None:
        self.server.terminate()
        self.server.wait()
        self.errors.close()

    def uri(self, path: str) -> str:
        return (self.root / path).as_uri()

    def send(self, message: dict) -> None:
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        self.server.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        self.server.stdin.flush()

    def notify(self, method: str, arguments: dict) -> None:
        self.send({"method": method, "params": arguments})

    def receive(self) -> dict:
        length = 0
        while header := self.server.stdout.readline().strip():
            name, _, number = header.partition(b": ")
            if name == b"Content-Length":
                length = int(number)
        if not length:
            self.errors.seek(0)
            reported = self.errors.read().decode(errors="replace").strip()
            stop(f"{' '.join(SERVER)} closed its output: {reported[-2000:]}")
        return json.loads(self.server.stdout.read(length))

    def request(self, method: str, arguments: dict):
        request_id = next(self.request_ids)
        self.send({"id": request_id, "method": method, "params": arguments})
        while True:
            message = self.receive()
            if "method" not in message and message.get("id") == request_id:
                if "error" in message:
                    stop(f"pyright {method}: {message['error']['message']}")
                return message["result"]
            if "method" in message and "id" in message:
                self.send({"id": message["id"], "result": self.answer(message)})

    def answer(self, asked: dict) -> list[dict] | None:
        """The reply to a request the server makes: the settings of each
        section it asks for, and nothing for any other request."""
        if asked["method"] != "workspace/configuration":
            return None
        return [
            self.settings.get(item.get("section"), {})
            for item in asked["params"]["items"]
        ]

    def position(self, site: Site) -> dict:
        text = self.lines[site.path][site.line - 1]
        return {
            "textDocument": {"uri": self.uri(site.path)},
            "position": {
                "line": site.line - 1,
                "character": utf16_column(text, site.column),
            },
        }

    def definition_site(self, path: str, node) -> Site:
        """The site of the name a def or class statement declares."""
        located = token_name_site(
            self.lines[path], node.lineno, node.col_offset, [node.name]
        )
        return Site(path, *located)

    def file(self, uri: str) -> tuple[str | None, Path, list[str]]:
        """For a file the server names: its path when it is measured, where it
        is, and its lines."""
        location = Path(unquote(urlparse(uri).path))
        if location.is_relative_to(self.root):
            path = str(location.relative_to(self.root))
            if path in self.measured:
                return path, location, self.lines[path]
        if location not in self.outside_lines:
            with tokenize.open(location) as source:
                self.outside_lines[location] = LINE_END.split(source.read())
        return None, location, self.outside_lines[location]

    def declaration(self, asked: Site, location: dict) -> Declaration | None:
        """The declaration a server location names for the name asked about;
        None for a location that names none."""
        uri = location.get("uri") or location["targetUri"]
        span = location.get("range") or location["targetSelectionRange"]
        start, end = span["start"], span["end"]
        path, where, lines = self.file(uri)
        named = (start["line"], start["character"], end["character"])
        if start["line"] >= len(lines) or named in self.imported.get(path, ()):
            return None
        text = lines[start["line"]]
        units = text.encode("utf-16-le")
        if start["character"] * 2 > len(units):
            return None
        last = end["character"] if end["line"] == start["line"] else len(units) // 2
        spanned = units[start["character"] * 2 : last * 2].decode(
            "utf-16-le", "replace"
        )
        line = self.lines[asked.path][asked.line - 1]
        name = WORD.match(line, len(line.encode()[: asked.column].decode()))
        binding = self.imported[asked.path].get(named, ())
        if name and name.group() in binding and spanned not in binding:
            return None
        if path is not None:
            column = byte_column(text, start["character"])
            return Declaration(Site(path, start["line"] + 1, column), "", "")
        leading = WORD.match(spanned)
        name = leading.group() if leading else spanned
        return Declaration(None, outside_module(where), name)

    def declarations(self, site: Site) -> list[Declaration]:
        """Each declaration of the name at the site; empty when pyright cannot
        place the name."""
        if site not in self.declared:
            found = self.request("textDocument/definition", self.position(site))
            found = found or []
            named = [
                self.declaration(site, location)
                for location in (found if isinstance(found, list) else [found])
            ]
            self.declared[site] = [declared for declared in named if declared]
        return self.declared[site]

    def printed_type(self, site: Site) -> str:
        """The type pyright gives the name at the site, as it prints it; empty
        when it gives none."""
        if site not in self.types:
            hovered = self.request("textDocument/hover", self.position(site))
            shown = hovered["contents"]["value"] if hovered else ""
            first = shown.splitlines()[0] if shown else ""
            self.types[site] = first.partition(": ")[2]
        return self.types[site]

    def unanalysed(self, path: str, node: ast.AST) -> bool:
        """Whether the node lies in code pyright does not analyse. The server
        marks such a range unnecessary, as it does a name nothing reads, so
        for an expression only a range that runs beyond it is one."""
        if path not in self.skipped:
            located = {"textDocument": {"uri": self.uri(path)}}
            reported = self.request("textDocument/diagnostic", located)
            self.skipped[path] = [
                (
                    (marked["start"]["line"], marked["start"]["character"]),
                    (marked["end"]["line"], marked["end"]["character"]),
                )
                for marked in (
                    item["range"]
                    for item in reported.get("items", [])
                    if UNNECESSARY in item.get("tags", ())
                )
            ]
        lines = self.lines[path]
        start = (
            node.lineno - 1,
            utf16_column(lines[node.lineno - 1], node.col_offset),
        )
        end = (
            node.end_lineno - 1,
            utf16_column(lines[node.end_lineno - 1], node.end_col_offset),
        )
        whole = isinstance(node, ast.stmt)
        return any(
            first <= start and end <= last and (whole or (first, last) != (start, end))
            for first, last in self.skipped[path]
        )

    def namespace(self, path: str, module: str) -> bool:
        """Whether the module is a package without a file of its own: a
        directory beside the importing file or where imports are searched."""
        folders = [(self.root / path).parent, *self.searched]
        return any((folder / module.replace(".", "/")).is_dir() for folder in folders)

    def unresolved_imports(self, trees: dict[str, ast.Module]) -> dict[str, list[str]]:
        """Per measured file, each module it imports that pyright places in no
        file: one nothing searched provides, or one that ships no source or
        stub to read."""
        found: dict[str, list[str]] = {}
        for path, tree in trees.items():
            for node in ast.walk(tree):
                for line, column, module in imported_modules(node, self.lines[path]):
                    placed = self.declarations(Site(path, line, column))
                    if placed or self.namespace(path, module):
                        continue
                    if not self.unanalysed(path, node):
                        found.setdefault(path, []).append(
                            f"line {line}: {module} resolves to no file"
                        )
        return found
