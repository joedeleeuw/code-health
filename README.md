# code-health

Static analysis and ratchet for Python and Nix repositories, plus a Rust preset for Clippy and ast-grep.

`code-health` enforces code quality limits (file length, cyclomatic complexity, nesting depth, parameter counts), deep-module invariants, structural duplication checks, AST import cycles, and curated `ast-grep` rules. Excess items must be pinned in `.code-health.json`, where pins act as ceilings that ratchet down and can never be raised.

## Prerequisites

- Python `>=3.12`
- `uv` (including `uvx`, used to run `basedpyright` for Python unused-code resolution)
- `ast-grep` on `PATH`

## Rust preset

Rust is checked by the tools a Rust workspace already runs. `code-health` runs no Rust build and no Clippy; `presets/rust` holds what to adopt:

1. Copy `presets/rust/clippy.toml` to the workspace root (or merge it into yours). It sets the complexity, nesting and argument thresholds and the banned methods. Clippy counts `self` as an argument and the function body as one nesting level.
2. Paste `presets/rust/cargo-config.toml` into the workspace's `.cargo/config.toml`. It defines `cargo lint`: Clippy over every crate and target with the preset's lints and rustc's dead-code and unused warnings denied. The levels ride on the command, so no crate has to opt in.
3. Run the pattern rules with `ast-grep scan --config <checkout>/presets/rust/sgconfig.yml`.

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

### 1. Initialize Baseline

In your repository root, initialize `.code-health.json` with your project's baseline:

```bash
code-health --baseline
```

This generates `.code-health.json` with default limits and records existing violations as ratchet pins. Stage and commit this file:

```bash
git add .code-health.json
git commit -m "chore: initialize code-health baseline"
```

### 2. Run the Ratchet

Run `code-health` directly against staged/tracked files:

```bash
code-health
```

- **Exit 0**: All files satisfy configured limits and ratchet pins.
- **Exit 1**: Limit exceeded, or a pin was lowered in-place (stage the lowered `.code-health.json` to lock in improvements).
- **Exit 2**: Environment/configuration error (unreadable file, missing dependency, etc.).

## Pre-commit Integration

Add `code-health` to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: code-health
        name: code-health ratchet
        entry: uv run code-health
        language: system
        pass_filenames: false
        always_run: true
```

Or when installed as a standalone global tool:

```yaml
repos:
  - repo: local
    hooks:
      - id: code-health
        name: code-health ratchet
        entry: code-health
        language: system
        pass_filenames: false
        always_run: true
```

## Configuration (`.code-health.json`)

```json
{
  "exclude": [
    "vendor/*",
    "docs/*"
  ],
  "allowed": {},
  "limits": {
    "file_lines": 1000,
    "complexity": 25,
    "nesting": 4,
    "params": 5,
    "docstring_code": 0,
    "clone_pairs": 0
  },
  "pins": {}
}
```

- **`exclude`**: Globs to ignore from analysis.
- **`allowed`**: Maps `ast-grep` rule IDs to allowed file globs.
- **`limits`**: Invariant ceilings.
- **`pins`**: Recorded baseline pins that decrease automatically as code is refactored.
