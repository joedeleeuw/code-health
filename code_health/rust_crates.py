"""Rust crate identity and dependency names from Cargo manifests.

A source file belongs to the package of the nearest Cargo.toml above it; a file
with none is its own crate root and owns its directory. A package's external
crates are the sysroot crates plus every dependency key its manifest declares,
renames included, with dashes read as underscores.
"""

from functools import cache
from pathlib import Path, PurePosixPath
import tomllib

from code_health import stop

MANIFEST = "Cargo.toml"
SYSROOT_CRATES = frozenset({"std", "core", "alloc", "proc_macro", "test"})
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")


def declared_dependencies(document: dict) -> set[str]:
    tables = [document, *document.get("target", {}).values()]
    return {
        name.replace("-", "_")
        for table in tables
        for section in DEPENDENCY_TABLES
        for name in table.get(section, {})
    }


class RustCrates:
    def __init__(self, root: Path):
        self.root = root

    @cache
    def owner(self, folder: PurePosixPath) -> PurePosixPath | None:
        if (self.root / folder / MANIFEST).is_file():
            return folder
        if folder == folder.parent:
            return None
        return self.owner(folder.parent)

    def crate(self, path: str) -> str:
        folder = PurePosixPath(path).parent
        owner = self.owner(folder)
        return str(folder if owner is None else owner)

    @cache
    def external(self, crate: str) -> frozenset[str]:
        manifest = self.root / crate / MANIFEST
        if not manifest.is_file():
            return SYSROOT_CRATES
        try:
            document = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            stop(f"cannot read {crate}/{MANIFEST}: {error}")
        return SYSROOT_CRATES | declared_dependencies(document)
