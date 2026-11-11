# code-health

Static analysis for Python and Nix repositories, plus a Rust preset for Clippy and ast-grep.

`code-health` enforces code quality limits (file length, cyclomatic complexity, nesting depth, parameter counts), deep-module invariants, structural duplication checks, AST import cycles, and curated `ast-grep` rules. Every finding is an error; a repository accepts one by allowing that check in that file in its `pyproject.toml`.

## Prerequisites

- Python `>=3.12`
- `uv` (including `uvx`, used to run `basedpyright` for Python unused-code resolution)
- `ast-grep` on `PATH`

## Rust preset

Rust is checked by the tools a Rust workspace already runs. `code-health` runs no Rust build and no Clippy; `presets/rust` holds what to adopt:

1. Copy `presets/rust/clippy.toml` to the workspace root (or merge it into yours). It sets the complexity, nesting and argument thresholds and the banned methods. Clippy counts `self` as an argument and the function body as one nesting level.
2. Paste `presets/rust/cargo-config.toml` into the workspace's `.cargo/config.toml`. It defines `cargo lint`: Clippy over every crate and target with the preset's lints and rustc's dead-code and unused warnings denied. The levels ride on the command, so no crate has to opt in.
3. Run the pattern rules with `ast-grep scan --config <checkout>/presets/rust/sgconfig.yml`.
4. Install `cargo-machete` and run `cargo machete` for dependencies no code uses. A deliberate exception goes in the crate's `Cargo.toml` under `[package.metadata.cargo-machete]` as `ignored = ["name"]`.

`vague-name-rust` is off by default; `presets/rust/sgconfig-off.yml` runs it.

`cargo lint` then fails on the preset's lints. The pattern rule `suppressed-limit-rust` reports an `allow` or `expect` of one of those lints that states no reason.

## Installation

Install directly from Git using `uv`:

```bash
# As a standalone CLI tool
uv tool install git+https://github.com/joedeleeuw/code-health

# Pinned to a specific commit or tag
uv tool install git+https://github.com/joedeleeuw/code-health@<rev>

# Or run directly without installing
uvx --from git+https://github.com/joedeleeuw/code-health code-health
```

## Quick Start

Run it in a repository root. It needs no setup and writes nothing:

```bash
code-health
```

- **Exit 0**: nothing found outside the allowed files.
- **Exit 1**: one line per finding on stderr.
- **Exit 2**: the check could not run (unreadable file, failed tool, missing dependency).

Every finding is an error. Fix it, or allow that check in that file (see Configuration).

## Hook Integration

Call it from the hook runner the repository already uses. With pre-commit:

```yaml
repos:
  - repo: local
    hooks:
      - id: code-health
        name: code-health
        entry: code-health
        language: system
        pass_filenames: false
        always_run: true
```

With lefthook:

```yaml
pre-commit:
  jobs:
    - name: code-health
      run: code-health
```

## Configuration (`pyproject.toml`)

Settings live in the `[tool.code-health]` table. Every key is optional; a repository without the table gets the defaults shown for `limits`.

```toml
[tool.code-health]
exclude = ["vendor/*", "docs/*"]

[tool.code-health.limits]
file_lines = 1000
complexity = 25
nesting = 4
params = 5
docstring_code = 0

[tool.code-health.allowed]
single_call_hops = ["scripts/migrate.py"]
vague-name-python = ["tests/*"]
```

- **`exclude`**: Globs left out of the analysis.
- **`limits`**: The size limits.
- **`allowed`**: Maps a check name (the word before the colon in a finding, such as `single_call_hops`, `unused`, `nesting`) or a rule ID (ast-grep rule IDs, `deadnix`, `statix-<code>`) to file globs. A finding of that check in a matching file is not reported. A glob that matches no measured file stops the check.
