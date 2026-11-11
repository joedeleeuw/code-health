# code-health

Static analysis and ratchet engine for Python, Rust, and Nix repositories.

`code-health` enforces code quality limits (file length, cyclomatic complexity, nesting depth, parameter counts), deep-module invariants, structural duplication checks, AST import cycles, and curated `ast-grep` rules. Excess items must be pinned in `.code-health.json`, where pins act as ceilings that ratchet down and can never be raised.

## Installation

Install using `uv` or `pip`:

```bash
# As a CLI tool
uv tool install code-health

# Or into an active virtual environment
uv pip install code-health
# or
pip install code-health
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
