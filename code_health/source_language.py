"""Source language from a filename or an executable interpreter declaration."""

import re
from pathlib import Path, PurePath

SUFFIXES = {
    ".py": "Python",
    ".pyi": "Python",
    ".sh": "Bash",
    ".bash": "Bash",
    ".rs": "Rust",
    ".json": "Json",
    ".jsonc": "Json",
    ".yaml": "Yaml",
    ".yml": "Yaml",
}
INTERPRETERS = {
    "Python": re.compile(r"\b(?:python(?:3(?:\.\d+)?)?\b|uv\s+run\b)"),
    "Bash": re.compile(r"\b(?:bash|sh)\b"),
}


def source_language(path: str, first_line: str) -> str | None:
    suffix = PurePath(path).suffix
    if suffix:
        return SUFFIXES.get(suffix)
    if first_line.startswith("#!"):
        for language, pattern in INTERPRETERS.items():
            if pattern.search(first_line):
                return language
    return None


def source_languages(root: Path, files: list[str]) -> dict[str, str | None]:
    languages = {}
    for path in files:
        with (root / path).open("rb") as source:
            first_line = source.readline(256).decode("utf-8", errors="replace")
        languages[path] = source_language(path, first_line)
    return languages
