"""Reference metrics over every measured file: repository paths named in Python
strings and docstrings, JSON and YAML string values and markdown code spans that
resolve to nothing, and JSON objects whose keys shadow the file's own leaves.

A path is a slash-joined token that ends in a known extension, or a directory
written with a trailing slash under a top-level directory of the repository,
or an extensionless command beside tracked executables. It
resolves when it names a tracked file or directory, from the repository root or
from the naming file's directory, or when it is the tail of a tracked path. A
path whose parent directory exists in neither place names another repository's
file. Tokens next to a placeholder or glob, tokens with a date or counter
placeholder segment such as YYYY-MM-DD, absolute and home paths, URLs and paths
git ignores are not references. Markdown links belong to the link checker. Test,
mock, fixture, evidence and generated trees hold example paths, not references.
"""

import ast
import json
import posixpath
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from code_health import counted, run
from code_health.markdown_references import MarkdownReferences


def without_comments(text: str) -> str:
    characters = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            characters.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            characters.append(char)
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise ValueError("unterminated JSONC block comment")
            index = end + 2
            continue
        characters.append(char)
        index += 1
    return "".join(characters)


def without_trailing_commas(text: str) -> str:
    characters = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            characters.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "]}":
                index += 1
                continue
        characters.append(char)
        index += 1
    return "".join(characters)


def load_jsonc(path: Path) -> object:
    return json.loads(
        without_trailing_commas(without_comments(path.read_text(encoding="utf-8")))
    )


EXTENSIONS = frozenset(
    "py pyi md json jsonc jsonl yaml yml toml txt sh bash zsh fish ts tsx js"
    " mjs cjs jsx rs go c h cpp sql html css csv ipynb cfg ini conf j2 patch"
    " service timer"
    " plist xml lua nix".split()
)
TOKEN = re.compile(
    r"(?<![\w./~:$@%-])(?:\./)?\.?\w[\w.-]*"
    r"(?:/[\w.-]+(?:@\.(?:service|socket|timer|target|path|mount|slice|scope)(?:\.d)?)?)+/?"
)
PARTIAL = frozenset("*{<$%[\\")
PLACEHOLDER = re.compile(
    r"^(YYYY|YY|MM|DD|HH|N{2,}|X{2,})([-_](YYYY|YY|MM|DD|HH|N{2,}|X{2,}))*$"
)
SHADOWING = 3
EXAMPLE_TREES = frozenset(
    "test tests __tests__ __mocks__ mocks __fixtures__ fixtures testdata test-data"
    " evidence artifacts".split()
)
GENERATED_TREE = frozenset({"generated", "__generated__", "snapshots", "__snapshots__"})


class Loader(yaml.SafeLoader):
    """Safe YAML that reads application tags as their plain node."""


def untagged(loader: Loader, _suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


Loader.add_multi_constructor("", untagged)


def string_leaves(node) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from string_leaves(value)
    elif isinstance(node, list):
        for value in node:
            yield from string_leaves(value)


def relative_parts(tree: ast.AST) -> set[int]:
    """F-string fragments and strings joined onto a base path with a slash."""
    parts = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts.update(id(value) for value in node.values)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts.add(id(node.right))
    return parts


def python_strings(tree: ast.Module) -> Iterator[str]:
    """String constants, docstrings included, that stand as whole paths."""
    parts = relative_parts(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in parts:
                yield node.value


class Unparsable(Exception):
    """A data file that is not valid JSON or YAML."""


def parsed(root: Path, path: str):
    """The JSON or YAML documents of a data file."""
    try:
        text = (root / path).read_text()
        if path.endswith(".jsonc"):
            return [load_jsonc(root / path)]
        if path.endswith(".json"):
            return [json.loads(text)]
        return list(yaml.load_all(text, Loader))
    except (ValueError, yaml.YAMLError) as error:
        raise Unparsable(str(error)) from error


def strings(root: Path, path: str, documents: list, repository: Path) -> Iterator[str]:
    if path.endswith(".md"):
        yield from MarkdownReferences((root / path).read_text(), path, repository)
    for document in documents:
        if isinstance(document, ast.Module):
            yield from python_strings(document)
        else:
            yield from string_leaves(document)


def references(text: str, top: set[str]) -> Iterator[str]:
    for match in TOKEN.finditer(text):
        after = text[match.end() : match.end() + 1]
        written = match.group().removeprefix("./").rstrip(".")
        token = written.rstrip("/")
        if after in PARTIAL or "/" not in token or ".." in token.split("/"):
            continue
        if any(PLACEHOLDER.match(part.partition(".")[0]) for part in token.split("/")):
            continue
        name = token.rsplit("/", 1)[1]
        if after == "@" and "." not in name:
            continue
        if "." in name and name.rpartition(".")[2] in EXTENSIONS:
            yield token
        elif token.split("/", 1)[0] in top:
            yield written


def tracked_paths(files: list[str]) -> set[str]:
    known = set()
    for path in files:
        while path and path not in known:
            known.add(path)
            path = posixpath.dirname(path)
    return known


def tails(files: list[str]) -> set[str]:
    """Every shortened form of a tracked path, so an abbreviated name resolves."""
    found = set()
    for path in files:
        parts = path.split("/")
        found.update("/".join(parts[start:]) for start in range(1, len(parts)))
    return found


def example_file(path: str) -> bool:
    """A file in a test, mock, fixture, evidence or generated tree, or a test."""
    folders = path.split("/")[:-1]
    if {folder.lower() for folder in folders} & EXAMPLE_TREES:
        return True
    if any(folder.lower() in GENERATED_TREE for folder in folders):
        return True
    name = posixpath.basename(path)
    stem = name.split(".", 1)[0]
    return name == "conftest.py" or stem.startswith("test_") or stem.endswith("_test")


def dead_paths(
    root: Path, documents_by_path: dict[str, list], listed: list[str]
) -> dict:
    """Per file, the references that resolve to nothing."""
    known = tracked_paths(listed)
    git_directory = run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root
    ).strip()
    repository = Path(git_directory).parent
    shortened = tails(listed)
    executable_parents = {
        posixpath.dirname(path)
        for path in listed
        if (root / path).stat().st_mode & 0o111
    }
    top = {path for path in known if "/" not in path and (root / path).is_dir()}
    found: dict[str, list[tuple[str, str]]] = {}
    for path, documents in documents_by_path.items():
        if example_file(path):
            continue
        here = posixpath.dirname(path)
        for text in strings(root, path, documents, repository):
            for written in references(text, top):
                token = written.rstrip("/")
                command = posixpath.dirname(token) in executable_parents
                if not posixpath.splitext(token)[1] and not (
                    written.endswith("/") or command
                ):
                    continue
                beside = posixpath.normpath(posixpath.join(here, token))
                if {token, beside} & known or token in shortened:
                    continue
                parent = posixpath.dirname(token)
                if {parent, posixpath.dirname(beside)} & known:
                    found.setdefault(path, []).append((token, beside))
    unique = {form for refs in found.values() for ref in refs for form in ref}
    listing = "".join(f"{form}\n{form}/\n" for form in sorted(unique))
    ignored = {
        form.rstrip("/")
        for form in run(
            ["git", "check-ignore", "--stdin"], root, (0, 1), listing
        ).split()
    }
    live = {
        path: [token for token, beside in refs if not {token, beside} & ignored]
        for path, refs in found.items()
    }
    return {path: refs for path, refs in live.items() if refs}


def leaves(node, path: tuple = ()) -> Iterator[tuple]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, (*path, key))
    elif isinstance(node, list):
        for value in node:
            yield from leaves(value, path)
    else:
        yield path


def nested_dicts(node, path: tuple = ()) -> Iterator[tuple[tuple, dict]]:
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from nested_dicts(value, (*path, key))
    elif isinstance(node, list):
        for value in node:
            yield from nested_dicts(value, path)


def shadows(document) -> list[str]:
    """Objects with at least three dotted keys that name leaves outside them."""
    every = list(leaves(document))
    found = []
    for at, node in nested_dicts(document):
        named = [key for key in node if "." in key]
        if len(named) < SHADOWING:
            continue
        outside = {
            leaf[start:]
            for leaf in every
            if leaf[: len(at)] != at
            for start in range(len(leaf) - 1)
        }
        named = [key for key in named if tuple(key.split(".")) in outside]
        if len(named) >= SHADOWING:
            found.append(f"{'.'.join(at) or '<root>'}: {', '.join(named[:3])}")
    return found


def measure_references(
    root: Path, files: list[str], listed: list[str], trees: dict[str, ast.Module]
) -> tuple[dict, dict]:
    """The dead_paths, shadow_key_prose and unparsable counts, and the findings
    behind them. A data file that does not parse is a finding and is left out
    of the other two."""
    documents_by_path: dict[str, list] = {}
    unparsable: dict[str, list[str]] = {}
    for path in files:
        if path.endswith(".md"):
            documents_by_path[path] = []
        elif path.endswith((".json", ".jsonc", ".yaml", ".yml")):
            try:
                documents_by_path[path] = parsed(root, path)
            except Unparsable as error:
                unparsable[path] = [str(error)]
    documents_by_path.update({path: [tree] for path, tree in trees.items()})
    shadowed = {
        path: [found for document in documents for found in shadows(document)]
        for path, documents in documents_by_path.items()
        if path.endswith((".json", ".jsonc"))
    }
    findings = {
        "dead_paths": dead_paths(root, documents_by_path, listed),
        "shadow_key_prose": {path: found for path, found in shadowed.items() if found},
        "unparsable": unparsable,
    }
    return {name: counted(found) for name, found in findings.items()}, findings
