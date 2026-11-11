"""The manifests a repository declares its projects in, each read and decoded
once: pyproject.toml and the other TOML files as documents, setup.cfg and the
other ini files as raw configuration (no interpolation), and setup.py as the
measured tree of that file. A manifest is the file at a repository-relative
path; an absent one is None. A manifest that is not UTF-8 or does not decode
stops the check naming the file, and so does a field a reader requires to be a
table, a string or a list of strings that is not one.
"""

import ast
import configparser
import posixpath
import re
import tomllib
from pathlib import Path

from code_health import stop


def separated(text: str) -> list[str]:
    """An ini list value: entries separated by commas or whitespace."""
    return [part for part in re.split(r"[\s,]+", text) if part]


def called_name(call: ast.Call) -> str:
    """The name a call spells for its callee: a plain name or the last
    attribute."""
    return getattr(call.func, "id", getattr(call.func, "attr", ""))


def config_table(config: Path, field_name: str, declared) -> dict:
    if not isinstance(declared, dict):
        stop(f"{config}: {field_name} is not a table")
    return declared


def text(config: Path, field_name: str, declared) -> str:
    if not isinstance(declared, str):
        stop(f"{config}: {field_name} is not a string")
    return declared


def strings(config: Path, field_name: str, declared) -> list[str]:
    if not isinstance(declared, list) or not all(isinstance(v, str) for v in declared):
        stop(f"{config}: {field_name} is not a list of strings")
    return declared


class TomlManifest:
    """One decoded TOML manifest and the settings its optional tables hold."""

    def __init__(self, path: Path, document: dict):
        self.path = path
        self.document = document

    def setting(self, *keys: str):
        """The value under a path of optional tables, None where a table or the
        key is absent; a path through a value that is not a table stops the
        check naming the file and the field."""
        nested = self.document
        for depth, key in enumerate(keys):
            config_table(self.path, ".".join(keys[:depth]), nested)
            if key not in nested:
                return None
            nested = nested[key]
        return nested


class Manifests:
    """The repository's manifests, decoded on demand and remembered."""

    def __init__(self, root: Path, trees: dict[str, ast.Module]):
        self.root = root
        self.trees = trees
        self.texts: dict[str, str | None] = {}
        self.documents: dict[str, TomlManifest | None] = {}
        self.parsers: dict[str, configparser.RawConfigParser | None] = {}

    def text(self, source: str) -> str | None:
        if source not in self.texts:
            manifest = self.root / source
            try:
                self.texts[source] = (
                    manifest.read_bytes().decode() if manifest.is_file() else None
                )
            except UnicodeDecodeError as error:
                stop(f"{source}: {error}")
        return self.texts[source]

    def toml(self, source: str) -> TomlManifest | None:
        if source not in self.documents:
            content = self.text(source)
            try:
                self.documents[source] = (
                    None
                    if content is None
                    else TomlManifest(self.root / source, tomllib.loads(content))
                )
            except tomllib.TOMLDecodeError as error:
                stop(f"{source}: {error}")
        return self.documents[source]

    def ini(self, source: str) -> configparser.RawConfigParser | None:
        if source not in self.parsers:
            content = self.text(source)
            parser = configparser.RawConfigParser()
            try:
                if content is not None:
                    parser.read_string(content, source)
            except configparser.Error as error:
                stop(f"{source}: {error}")
            self.parsers[source] = None if content is None else parser
        return self.parsers[source]

    def setup_py(self, folder: str) -> ast.Module | None:
        return self.trees.get(posixpath.join(folder, "setup.py"))
