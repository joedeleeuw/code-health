# Repository Agent Instructions

## Commands

- `uv run code-health`: Run the whole-workspace code-health checks against this checkout.
- `uvx ruff@0.15.12 check --force-exclude`: Lint Python sources against `ruff.toml`.
- `ast-grep test --config code_health/sgconfig.yml --skip-snapshot-tests`: Run the ast-grep rule test suite.

## Workspace Boundaries

- `code_health/` owns the CLI entry point (`code_health.cli:main`), the Python, Nix, and reference analyzers, the retired-rule ledger (`code_health.rule_ledger`, `retired-rule-ids.json`), and the pattern rules: `rules/<language>/` (default), `rules-off/` (opt-in), `utils/`, run by `sgconfig.yml` and `sgconfig-off.yml`.
- `presets/rust/` holds what a Rust workspace adopts itself (`clippy.toml`, `cargo-config.toml`, `workspace-lints.toml`, and `sgconfig.yml` over `code_health/rules/rust`); `presets/python/ruff.toml` the ruff settings a Python repository adopts. `code-health` runs no Rust build.
- `corpus.json` lists the measurement corpus by remote and commit; `corpus_rates.py CLONE_ROOT...` prints each Rust rule's hit rate per role over it. Neither ships in the package.
