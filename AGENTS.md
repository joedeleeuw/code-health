# Repository Agent Instructions

## Commands

- `uv run code-health`: Run the whole-workspace code-health ratchet against this checkout.
- `uvx ruff@0.15.12 check --force-exclude`: Lint Python sources against `ruff.toml`.
- `ast-grep test --config code_health/config/sgconfig.yml --skip-snapshot-tests`: Run the ast-grep rule test suite.

## Workspace Boundaries

- `code_health/` owns the CLI entry point (`code_health.cli:main`), the Python, Nix, and reference analyzers, the retired-rule ledger (`code_health.rule_ledger`), and the packaged configuration under `code_health/config/` (`sgconfig.yml`, `retired-rule-ids.json`, `rules/`, `rule-tests/`, `utils/`, `corpus-clones.json`).
- `presets/rust/` owns the Rust preset a workspace adopts itself: `clippy.toml`, `cargo-config.toml`, and the ast-grep config over `code_health/config/rules/rust/`. `code-health` runs no Rust build.
