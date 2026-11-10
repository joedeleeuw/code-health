"""Uses of Python declarations from dotted references in Python strings and
tracked non-Python files, and from attribute reads in Jinja2 and Django
templates and template strings.

A dotted or colon reference that starts with one of the repository's own
modules and resolves through the module table uses the declaration it names and
each object on the path, followed through package and module re-exports to the
defining file. A colon reference whose target calls a factory uses the
callable, and adjacent string literals in a tuple or call whose first string
names a repository module and whose second names an object or call in it are the
split form of a colon reference. Code, configuration, data, template, script,
container and process files count for any declaration in the repository; prose
documentation counts only for public declarations of modules the repository
ships or whose shipping does not resolve. A tracked file is read as the text
it holds, whatever format its name claims: a fixture that is malformed on
purpose, a data file with comments and a log are searched like any other, and
one that is binary or does not decode holds no reference.

Tracked template files and Python string literals outside docstrings that
contain template tags are lexed with Jinja2; one that does not lex is another
template language and has no attribute reads. Every attribute read in a
template or template string counts as a use of every member with that name,
assigned attributes included.
"""

import ast
import posixpath
import re
import tokenize
import warnings
from itertools import pairwise
from pathlib import Path

import jinja2

from code_health.forwarding import documented
from code_health.imports import module_name
from code_health.repository import SOURCE_ROOTS, split_reference
from code_health.source_language import source_languages
from code_health.specimens import Specimens
from code_health.symbols import FUNCTIONS

TEMPLATE_ENVIRONMENT = jinja2.Environment()
TEMPLATE_SUFFIXES = (
    ".html",
    ".htm",
    ".jinja",
    ".jinja2",
    ".j2",
    ".django",
    ".tpl",
    ".py-tpl",
    ".e-tpl",
    ".template",
)
PROSE_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
PROSE_NAMES = frozenset({"README", "CHANGELOG", "HISTORY", "LICENSE", "NOTICE"})
BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".whl",
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".wav",
    ".ogg",
    ".webm",
    ".sqlite",
    ".db",
)
REFERENCE_TOKEN = re.compile(
    r"(?<![\w/\\])"
    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+"
    r"(?::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\([^()\n]*\))?)?"
    r"|"
    r"(?<![\w/\\])"
    r"[A-Za-z_]\w*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\([^()\n]*\))?"
)


def unmeasured_parse(text: str, filename: str, mode: str = "exec"):
    """The tree of text the measurement leaves out; what Python warns about
    in it is not the measured code's to answer."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(text, filename, mode)


def call_stem(text: str) -> str:
    """The callable path before the opening parenthesis when the text is a
    call expression; the text itself when it has none."""
    stripped = text.strip()
    if "(" not in stripped:
        return stripped
    try:
        parsed_call = unmeasured_parse(stripped, "<reference>", "eval")
    except SyntaxError:
        return ""
    if isinstance(parsed_call.body, ast.Call):
        return ast.unparse(parsed_call.body.func)
    return ""


def reference_parts(text: str) -> tuple[list[str], int] | None:
    """The identifier parts of a dotted or colon reference, and the maximum
    number of components that may belong to the module path."""
    stripped = text.strip()
    if ":" in stripped:
        module_text, separator, target_text = stripped.partition(":")
        if not separator or ":" in target_text:
            return None
        target_stem = call_stem(target_text)
        if not module_text or not target_stem:
            return None
        module_parts = module_text.split(".")
        target_parts = target_stem.split(".")
        parts = [*module_parts, *target_parts]
        if all(part.isidentifier() for part in parts):
            return parts, len(module_parts)
        return None
    parts = split_reference(stripped)
    if parts is None or len(parts) < 2:
        return None
    return parts, len(parts) - 1


def lex_template_attributes(text: str) -> set[str]:
    """Every attribute name read after a dot operator in a Jinja2 or Django
    template."""
    tokens = list(TEMPLATE_ENVIRONMENT.lex(text))
    attributes: set[str] = set()
    for index, (_, kind, token_text) in enumerate(tokens):
        if kind != "operator" or token_text != ".":
            continue
        for _, next_kind, next_text in tokens[index + 1 :]:
            if next_kind == "whitespace":
                continue
            if next_kind == "name":
                attributes.add(next_text)
            break
    return attributes


class OutsidePython:
    """Dotted references and template attribute reads across Python strings and
    tracked non-Python files."""

    def __init__(self, root: Path, unmeasured: set[str], specimens: Specimens):
        self.root = root
        self.modules = specimens.modules
        self.shipped = specimens.shipped
        self.roots = list(
            dict.fromkeys(
                [
                    *self.shipped.import_roots,
                    *SOURCE_ROOTS,
                    *self.modules.search_roots(),
                ]
            )
        )
        self.top_names = {
            module_name(path, self.modules.known).split(".")[0]
            for path in specimens.trees
        } | self.modules.packages
        self.elsewhere: dict[str, str] = {}
        self.template_reads: set[str] = set()
        self.code_targets: set[tuple[str, str]] = set()
        self.documented_targets: set[tuple[str, str, str]] = set()
        for tree in specimens.trees.values():
            self.read_python_tree(tree)
        self.read_unmeasured(unmeasured)

    def resolve_reference(
        self, parts: list[str], limit: int, documented_only: bool
    ) -> None:
        """Record the declaration a dotted or colon reference resolves to."""
        if len(parts) < 2 or parts[0] not in self.top_names:
            return
        resolved = self.modules.dotted_module(parts, self.roots, limit)
        if resolved is None:
            return
        module_path, remainder = resolved
        if not remainder:
            return
        defining_path = self.modules.origin(module_path, remainder[0])
        target = ".".join(remainder)
        if documented_only:
            self.documented_targets.add((module_path, defining_path, target))
        else:
            self.code_targets.add((defining_path, target))

    def read_string_literal(self, text: str) -> None:
        """Extract template reads and dotted references from a Python string."""
        if ("{{" in text and "}}" in text) or ("{%" in text and "%}" in text):
            self.read_template(text)
        parsed_reference = reference_parts(text)
        if parsed_reference is not None:
            self.resolve_reference(parsed_reference[0], parsed_reference[1], False)
        elif "." in text or ":" in text:
            self.read_tokens(text, False)

    def read_template(self, text: str) -> None:
        """The attribute reads of a text that lexes as a Jinja2 or Django
        template."""
        try:
            self.template_reads |= lex_template_attributes(text)
        except jinja2.TemplateSyntaxError:
            return

    def read_adjacent_strings(self, elements: list[ast.expr]) -> None:
        """Resolve adjacent string literals (module, target) in a tuple or call."""
        strings = [
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        for first, second in pairwise(strings):
            first_parts = first.strip().split(".")
            if not all(part.isidentifier() for part in first_parts):
                continue
            stem = call_stem(second)
            if not stem:
                continue
            second_parts = stem.split(".")
            if all(part.isidentifier() for part in second_parts):
                self.resolve_reference(
                    [*first_parts, *second_parts], len(first_parts), False
                )

    def read_python_tree(self, tree: ast.Module) -> None:
        """Read non-docstring string literals and adjacent (module, target)
        pairs in a Python module."""
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, *FUNCTIONS))
            and node.body
            and documented(node)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    self.read_string_literal(node.value)
            elif isinstance(node, (ast.Tuple, ast.Call)):
                elements = node.elts if isinstance(node, ast.Tuple) else node.args
                self.read_adjacent_strings(elements)

    def read_tokens(self, text: str, documented_only: bool) -> None:
        """Extract dotted and colon references to repository modules from text."""
        for match in REFERENCE_TOKEN.finditer(text):
            before = text[match.start() - 1 : match.start()]
            after = text[match.end() : match.end() + 1]
            if before in ("/", "@") or after == "/":
                continue
            token = match.group()
            first = token.split(".", 1)[0].split(":", 1)[0]
            if first not in self.top_names:
                continue
            parsed_reference = reference_parts(token)
            if parsed_reference is not None:
                self.resolve_reference(
                    parsed_reference[0], parsed_reference[1], documented_only
                )

    def read_unmeasured(self, unmeasured: set[str]) -> None:
        """Read unmeasured Python files and tracked non-Python files once."""
        if not unmeasured:
            return
        languages = source_languages(self.root, sorted(unmeasured))
        for path, language in languages.items():
            if language == "Python":
                self.read_unmeasured_python(path)
            else:
                self.read_non_python(path)

    def read_unmeasured_python(self, path: str) -> None:
        try:
            with tokenize.open(self.root / path) as source:
                text = source.read()
        except (SyntaxError, UnicodeDecodeError):
            return
        self.elsewhere[path] = text
        try:
            tree = unmeasured_parse(text, path)
        except (SyntaxError, ValueError):
            self.read_tokens(text, False)
        else:
            self.read_python_tree(tree)

    def read_non_python(self, path: str) -> None:
        lower = path.lower()
        if lower.endswith(BINARY_SUFFIXES):
            return
        full_path = self.root / path
        with full_path.open("rb") as handle:
            head = handle.read(8192)
            if b"\0" in head:
                return
            raw = head + handle.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
        if lower.endswith(TEMPLATE_SUFFIXES):
            self.read_template(text)
        stem = posixpath.splitext(posixpath.basename(path))[0].upper()
        documented_only = lower.endswith(PROSE_SUFFIXES) or stem in PROSE_NAMES
        self.read_tokens(text, documented_only)

    def uses_target(self, path: str, loaded: str) -> bool:
        """Whether a dotted or colon reference outside Python resolves to or
        passes through the declaration."""
        prefix = f"{loaded}."
        for defining_path, target in self.code_targets:
            if defining_path == path and f"{target}.".startswith(prefix):
                return True
        if any(part.startswith("_") for part in loaded.split(".")):
            return False
        for module_path, defining_path, target in self.documented_targets:
            if defining_path != path or not f"{target}.".startswith(prefix):
                continue
            if (
                self.shipped.status(module_path) != "unshipped"
                or self.shipped.status(defining_path) != "unshipped"
            ):
                return True
        return False
