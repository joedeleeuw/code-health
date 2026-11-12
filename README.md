# code-health

Pattern rules and linter settings for Rust, Python, Bash, JSON, YAML and Markdown. There is no program to install: the rules are plain [ast-grep](https://ast-grep.github.io) rules, and the presets are settings files for Clippy and ruff.

## Use the rules

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

With pre-commit, pin a tag and the hook runs over the changed files. Leave files out with `exclude`:

```yaml
repos:
  - repo: https://github.com/joedeleeuw/code-health
    rev: v0.2.0
    hooks:
      - id: pattern-rules
        exclude: ^docs/field-notes/
```

- `code_health/sgconfig-off.yml` runs the rules under `rules-off/`, which are off by default: correct matches that a graded sample found mostly not worth changing.
- To run a subset, copy the rule files you want into your own rule directory. The Rust rules also need `code_health/utils/`.
- The `hash-literal-*` rules were withdrawn: they flagged every download checksum and content ID in data files.
- To accept a finding, put `// ast-grep-ignore: <rule-id>` (or `#` in Python, Bash and YAML) on the line above it.

## Python preset

`presets/python/ruff.toml` holds the [ruff](https://docs.astral.sh/ruff) settings that go with the Python rules: unused imports and locals, the complexity and argument limits, a ban on `contextlib.suppress`, blanket `# type: ignore` comments without an error code (`PGH003`), blocking HTTP/process/file/`time.sleep` calls inside `async def` outside test files (`ASYNC210`, `ASYNC220`, `ASYNC221`, `ASYNC230`, `ASYNC251`), and private-attribute access in test files (outside tests the rule is noise: mature libraries hit it more than agent-written code). `presets/python/ruff-off.toml` extends it with bans that are house policy and off by default: mocks and `@deprecated`. Point your own config at one of them with `extend = "../code-health/presets/python/ruff.toml"` (top level of `ruff.toml`, or under `[tool.ruff]` in `pyproject.toml`), or paste its tables into `ruff.toml`. In `pyproject.toml` each pasted table name needs the `tool.ruff.` prefix; without it ruff ignores the table silently.

## Rules

### Rust

| Rule | What it reports |
|---|---|
| `anemic-error-rust` | Signature or alias uses Result<T, String>, or .map_err erases a typed error into String; use a typed domain error enum or error crate type. |
| `blocking-in-async-rust` | Blocking thread::sleep, synchronous std::fs I/O, or synchronous std::process::Command execution inside an async function or block stalls the async runtime executor; use tokio::time::sleep, tokio::fs, tokio::process::Command, or spawn_blocking. |
| `catch-log-rethrow-rust` | Logging an error inside inspect_err or map_err and immediately propagating it unmodified with ? duplicates error logs across stack frames; add domain context or log once at the top-level boundary. |
| `closed-string-field-rust` (off by default) | A status, kind, state, level, mode, role or severity field is a String, so any text is a valid case. |
| `closed-string-rust` | A struct field stores a closed case space as String instead of an enum. |
| `compat-identifier-rust` | Compatibility/retirement name declared, or one receiver probed under a fallback key. |
| `dated-comment-rust` | Dated provenance prose in a comment. |
| `elif-chain-rust` | If/else-if chain with 3+ else-ifs. |
| `error-cause-erasure-rust` | .map_err wraps a formatted error string or anyhow! message instead of preserving the underlying error cause; carry #[source]/#[from] on a typed variant or use .context(...). |
| `error-string-branch-rust` | Control flow branches on substrings of a rendered error message instead of matching a typed error variant or structured status code. |
| `lock-unwrap-rust` (off by default) | A lock is taken with .unwrap(), so one panic while it is held poisons it and every later lock() panics too. |
| `secret-call-rust` | Direct secret-store call (secret-tool, op, security). |
| `silent-result-discard-rust` | Boundary serde/FromStr parse, filesystem I/O, or Result failure is silently collapsed into Option, an empty value, or discarded; propagate or handle the error explicitly. |
| `single-call-hop-rust` | Private function only forwards its parameters, in order, to one first-party call; call the target directly or give the wrapper a job. |
| `sql-string-interpolation-rust` | format! interpolates a value into SQL statement text (quoted or compared), bypassing bound parameters; pass values through the driver's bindings (?1, $1, params![]). |
| `sum-to-product-struct-rust` | Struct flattens a success/failure sum type into a boolean flag plus an Option<_> error field; model the mutually exclusive outcomes as Result<T, E> or a tagged enum. |
| `suppressed-limit-rust` | A size or safety lint from the preset is silenced with allow or expect and no reason; fix the code so the lint passes, change the limit for the whole workspace in clippy.toml, or state the reason in the attribute. |
| `unbounded-http-client-rust` | reqwest::get, reqwest::Client::new(), or an unconfigured ureq request/Agent executes HTTP with no timeout; configure Client::builder().timeout(...) or ureq timeout_global/timeout(...) so hung peers cannot stall callers indefinitely. |
| `untyped-json-response-rust` | Ok(json!({ ... })) returns an ad-hoc untyped JSON object instead of a closed #[derive(Serialize)] struct or enum. |
| `untyped-value-boundary-rust` | Raw serde_json::Value in a function parameter, return type, or struct field defers schema validation into callers and callees; use a closed #[derive(Serialize, Deserialize)] struct or enum. |
| `untyped-value-ingress-rust` | Boundary payload is decoded into an untyped Value, Table, or Mapping instead of a closed #[derive(Deserialize)] struct or enum; parse once at the boundary into the domain type. |
| `untyped-value-probe-rust` | Untyped Value key probe or open Map<String, Value> field, parameter, or return type defers schema validation past the boundary; decode once into a closed #[derive(Deserialize)] struct or enum. |
| `vague-name-rust` (off by default) | Vague declared name. |
| `value-roundtrip-rust` | Data is reparsed from serde_json::Value via from_value, round-tripped through JSON text, or built with json! and mutated by string key; decode once at the boundary into the typed Rust struct. |

### Python

| Rule | What it reports |
|---|---|
| `catch-log-rethrow-python` | Catching an exception only to log it and immediately re-raise duplicates error logs at every stack frame; add domain context with raise ... from e or log once at the top-level boundary. |
| `closed-string` | A str parameter or field is closed by a literal set at runtime. |
| `compat-identifier-python` | Compatibility/retirement name declared. |
| `dated-comment-python` | Dated provenance prose in a comment. |
| `dict-argument-write` | A function writes its result into a dict it was handed and returns nothing. |
| `elif-chain` | If/elif chain with 3+ elifs. |
| `error-string-branch-python` | Exception handler branches on substrings of str(err) instead of catching a specific exception type or checking a structured status or error-code attribute. |
| `guarded-import` | Guarded import. |
| `host-path-python` | Host-absolute path in a repository file (tracked or untracked; only ignored files are skipped). |
| `nested-ternary` | Nested conditional expression. |
| `path-prefix-containment-python` | String .startswith() on a resolved path without a trailing separator accepts sibling directories such as /root-backup; check containment with Path.is_relative_to() or os.path.commonpath(). |
| `reexport-module` | Module whose only statements import names from elsewhere. |
| `secret-call-python` | Direct secret-store call (secret-tool, op, security). |
| `silent-decode-discard-python` | Boundary JSON, YAML, or TOML decode failure is caught and collapsed into None or an empty value; let invalid input fail loudly or handle the parse error explicitly. |
| `sum-to-product-class-python` | Class flattens mutually exclusive success/failure outcomes into a boolean ok/success flag plus an optional error field; model distinct outcomes as a union of closed types or raise an exception. |
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

## Rust preset

Rust is checked by the tools a Rust workspace already runs; `presets/rust` holds what to adopt:

1. Copy `presets/rust/clippy.toml` to the workspace root (or merge it into yours). It sets the complexity, nesting and argument thresholds and bans `std::thread::sleep` across compiled code (`blocking-in-async-rust` also catches `thread::sleep` inside `async` without a build, while `Value`, `serde_json::from_value`, and boundary/parse `Result` checks live solely in the pattern rules). Clippy counts `self` as an argument and the function body as one nesting level.
2. Paste `presets/rust/cargo-config.toml` into the workspace's `.cargo/config.toml`. It defines `cargo lint`: Clippy over every crate's library and binary targets (`--lib --bins`, leaving unit and integration test targets, `examples`, and `benches` unchecked by Clippy) with the preset's lints and rustc's dead-code and unused warnings denied. The levels ride on the command, so no crate has to opt in. A workspace that vendors crates it does not want linted uses `presets/rust/workspace-lints.toml` instead: paste it into the root `Cargo.toml` and add `[lints] workspace = true` to each crate that should be checked; a crate without that line is skipped.
3. Run the pattern rules with `ast-grep scan --config <checkout>/presets/rust/sgconfig.yml`.
4. Install `cargo-machete` and run `cargo machete` for dependencies no code uses. A deliberate exception goes in the crate's `Cargo.toml` under `[package.metadata.cargo-machete]` as `ignored = ["name"]`.

`vague-name-rust`, `lock-unwrap-rust` and `closed-string-field-rust` are off by default: they match what they say, but in a graded sample most of their findings were not worth changing. `presets/rust/sgconfig-off.yml` runs them.

`cargo lint` then fails on the preset's lints. The pattern rule `suppressed-limit-rust` reports an `allow` or `expect` of one of those lints that states no reason.

## Licence

MIT. See `LICENSE`.
