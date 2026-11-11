# code-health

Static analysis for Python and Nix repositories, plus a Rust preset for Clippy and ast-grep.

`code-health` enforces code quality limits (file length, cyclomatic complexity, nesting depth, parameter counts), deep-module invariants, structural duplication checks, AST import cycles, and curated `ast-grep` rules. Every finding is an error; a repository accepts one by allowing that check in that file in its `pyproject.toml`.

## Use the rules alone

The pattern rules are plain [ast-grep](https://ast-grep.github.io) rules under `code_health/rules/<language>/`. They need a checkout of this repository and `ast-grep`, nothing else:

```bash
git clone https://github.com/joedeleeuw/code-health
cd your-repo
ast-grep scan --config ../code-health/code_health/sgconfig.yml
```

ast-grep applies each rule only to files of its language, so the one config serves any repository. A match exits 1, so the same line works as a hook. With lefthook:

```yaml
pre-commit:
  jobs:
    - name: pattern-rules
      run: ast-grep scan --config ../code-health/code_health/sgconfig.yml
```

- `code_health/sgconfig-off.yml` runs the rules under `rules-off/`, which are off by default: correct matches that a graded sample found mostly not worth changing.
- To run a subset, copy the rule files you want into your own rule directory. The Rust rules also need `code_health/utils/`.
- A rule that was shipped and then withdrawn is listed with the reason in `code_health/retired-rule-ids.json`; the `hash-literal-*` rules went that way because they flagged every download checksum and content ID in data files.
- To accept a finding, put `// ast-grep-ignore: <rule-id>` (or `#` in Python, Bash and YAML) on the line above it.

## Python preset

`presets/python/ruff.toml` holds the [ruff](https://docs.astral.sh/ruff) settings that go with the Python rules: unused imports and locals, the complexity and argument limits, and a ban on `contextlib.suppress`. `presets/python/ruff-off.toml` extends it with bans that are house policy and off by default: mocks and `@deprecated`. Point your own config at one of them with `extend = "../code-health/presets/python/ruff.toml"` (top level of `ruff.toml`, or under `[tool.ruff]` in `pyproject.toml`), or paste its tables into `ruff.toml`. In `pyproject.toml` each pasted table name needs the `tool.ruff.` prefix; without it ruff ignores the table silently.

## Rules

### Rust

| Rule | What it reports |
|---|---|
| `anemic-error-rust` | Function returns Result<T, String> or erases error types via .map_err(\|e\| e.to_string()); use a typed domain error enum or error crate type. |
| `blocking-in-async-rust` | Blocking thread::sleep, synchronous std::fs I/O, or synchronous std::process::Command execution inside an async function or block stalls the async runtime executor; use tokio::time::sleep, tokio::fs, tokio::process::Command, or spawn_blocking. |
| `closed-string-field-rust` (off by default) | A status, kind, state, level, mode, role or severity field is a String, so any text is a valid case. |
| `closed-string-rust` | A struct field stores a closed case space as String instead of an enum. |
| `compat-identifier-rust` | Compatibility/retirement name declared, or one receiver probed under a fallback key. |
| `dated-comment-rust` | Dated provenance prose in a comment. |
| `elif-chain-rust` | If/else-if chain with 3+ else-ifs. |
| `host-path-rust` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |
| `lock-unwrap-rust` (off by default) | A lock is taken with .unwrap(), so one panic while it is held poisons it and every later lock() panics too. |
| `secret-call-rust` | Direct secret-store call (secret-tool, op, security). |
| `silent-result-discard-rust` | Boundary serde decode, filesystem I/O, or Result failure is silently collapsed into Option, an empty value, or discarded; propagate or handle the error explicitly. |
| `single-call-hop-rust` | Private function only forwards its parameters, in order, to one first-party call; call the target directly or give the wrapper a job. |
| `sql-string-interpolation-rust` | format! interpolates a value into SQL statement text (quoted or compared), bypassing bound parameters; pass values through the driver's bindings (?1, $1, params![]). |
| `sum-to-product-struct-rust` | Struct flattens a success/failure sum type into a boolean flag plus an Option<_> error field; model the mutually exclusive outcomes as Result<T, E> or a tagged enum. |
| `suppressed-limit-rust` | A size or safety lint from the preset is silenced with allow or expect and no reason; fix the code so the lint passes, change the limit for the whole workspace in clippy.toml, or state the reason in the attribute. |
| `untyped-value-probe-rust` | Untyped Value key probe or open Map<String, Value> field/parameter defers schema validation past the boundary; decode once into a closed. |
| `vague-name-rust` (off by default) | Vague declared name. |

### Python

| Rule | What it reports |
|---|---|
| `closed-string` | A str parameter or field is closed by a literal set at runtime. |
| `compat-identifier-python` | Compatibility/retirement name declared. |
| `dated-comment-python` | Dated provenance prose in a comment. |
| `dict-argument-write` | A function writes its result into a dict it was handed and returns nothing. |
| `elif-chain` | If/elif chain with 3+ elifs. |
| `guarded-import` | Guarded import. |
| `host-path-python` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |
| `nested-ternary` | Nested conditional expression. |
| `reexport-module` | Module whose only statements import names from elsewhere. |
| `secret-call-python` | Direct secret-store call (secret-tool, op, security). |
| `test-imports-private` | A test imports a private name. |
| `vague-name-python` | Vague declared name. |

### Bash

| Rule | What it reports |
|---|---|
| `dated-comment-bash` | Dated provenance prose in a comment. |
| `host-path-bash` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |
| `ls-capture-assignment-bash` | Variable takes one path from ls output, so a glob that matches nothing or several entries assigns an empty or wrong value without failing. |
| `secret-call-bash` | Direct secret-store call (secret-tool, op, security). |

### JSON

| Rule | What it reports |
|---|---|
| `approval-key-json` | Approval/status key in a config. |
| `host-path-json` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |

### YAML

| Rule | What it reports |
|---|---|
| `approval-key-yaml` | Approval/status key in a config. |
| `host-path-yaml` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |

### Markdown

| Rule | What it reports |
|---|---|
| `host-path-markdown` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |

## Prerequisites

- Python `>=3.12`
- `uv` (including `uvx`, used to run `basedpyright` for Python unused-code resolution)
- `ast-grep` on `PATH`

## Rust preset

Rust is checked by the tools a Rust workspace already runs. `code-health` runs no Rust build and no Clippy; `presets/rust` holds what to adopt:

1. Copy `presets/rust/clippy.toml` to the workspace root (or merge it into yours). It sets the complexity, nesting and argument thresholds and the banned methods. Clippy counts `self` as an argument and the function body as one nesting level.
2. Paste `presets/rust/cargo-config.toml` into the workspace's `.cargo/config.toml`. It defines `cargo lint`: Clippy over every crate and target with the preset's lints and rustc's dead-code and unused warnings denied. The levels ride on the command, so no crate has to opt in. A workspace that vendors crates it does not want linted uses `presets/rust/workspace-lints.toml` instead: paste it into the root `Cargo.toml` and add `[lints] workspace = true` to each crate that should be checked; a crate without that line is skipped.
3. Run the pattern rules with `ast-grep scan --config <checkout>/presets/rust/sgconfig.yml`.
4. Install `cargo-machete` and run `cargo machete` for dependencies no code uses. A deliberate exception goes in the crate's `Cargo.toml` under `[package.metadata.cargo-machete]` as `ignored = ["name"]`.

`vague-name-rust`, `lock-unwrap-rust` and `closed-string-field-rust` are off by default: they match what they say, but in a graded sample most of their findings were not worth changing. `presets/rust/sgconfig-off.yml` runs them.

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
- **`allowed`**: Maps a check name (the word before the colon in a finding, such as `single_call_hops`, `unused`, `nesting`, `unparsable`) or a rule ID (ast-grep rule IDs, `deadnix`, `statix-<code>`) to file globs. A finding of that check in a matching file is not reported. A glob that matches no measured file stops the check.

## Licence

MIT. See `LICENSE`.
