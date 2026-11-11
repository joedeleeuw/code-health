# Repository Agent Instructions

## Commands

- `uvx ruff@0.15.12 check --force-exclude`: Lint Python sources against `ruff.toml`.
- `ast-grep test --config code_health/sgconfig.yml --skip-snapshot-tests`: Run the ast-grep rule test suite.

## Workspace Boundaries

- `code_health/` holds the pattern rules: `rules/<language>/` (default), `rules-off/` (opt-in), `utils/`, run by `sgconfig.yml` and `sgconfig-off.yml`. There is no program; `.pre-commit-hooks.yaml` and `hooks/pattern-rules` let a consumer run the rules as a pinned pre-commit hook.
- `presets/rust/` holds what a Rust workspace adopts itself (`clippy.toml`, `cargo-config.toml`, `workspace-lints.toml`, and `sgconfig.yml` over `code_health/rules/rust`); `presets/python/ruff.toml` the ruff settings a Python repository adopts.
- `corpus.json` lists the measurement corpus by remote and commit; `corpus_rates.py CLONE_ROOT...` prints each Rust rule's hit rate per role over it. Neither ships in the package.
