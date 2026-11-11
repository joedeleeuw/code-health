"""The one owner of retired published rule IDs: code_health/retired-rule-ids.json
maps each ID that code-health or process-review published and withdrew to why
it went and what replaced it. code-health and process-review both read it
through this module, so a consumer that still names the ID fails with the
reason, and no rule may take the ID again.
"""

import json
from pathlib import Path

import yaml

from code_health import stop

PACKAGE = Path(__file__).resolve().parent
LEDGER = PACKAGE / "retired-rule-ids.json"
SGCONFIG = PACKAGE / "sgconfig.yml"
SGCONFIG_OFF = PACKAGE / "sgconfig-off.yml"


def rule_files(sgconfig: Path) -> list[Path]:
    """Every rule file ast-grep loads for a config: each ruleDirs entry,
    walked recursively for .yml and .yaml."""
    config = yaml.safe_load(sgconfig.read_text())
    return sorted(
        path
        for folder in config["ruleDirs"]
        for path in (sgconfig.parent / folder).rglob("*")
        if path.suffix in {".yml", ".yaml"}
    )


def rule_ids(files: list[Path]) -> set[str]:
    return {
        document["id"]
        for path in files
        for document in yaml.safe_load_all(path.read_text())
        if document
    }


def catalog_rule_files() -> list[Path]:
    """The rule files code-health runs."""
    return rule_files(SGCONFIG)


def catalog_rule_ids() -> set[str]:
    """Every rule ID code-health runs."""
    return rule_ids(catalog_rule_files())


def ledger_records() -> dict[str, dict[str, str]]:
    """Each ID in the ledger with its reason and replacement. A rule file that
    takes a ledgered ID stops the check."""
    records = json.loads(LEDGER.read_text())
    shipped = catalog_rule_ids() | rule_ids(rule_files(SGCONFIG_OFF))
    reused = sorted(shipped & records.keys())
    if reused:
        stop(f"rule ID {reused[0]} is listed in {LEDGER}")
    return records


def ledger_message(rule_id: str, records: dict[str, dict[str, str]]) -> str:
    """The reason a consumer naming a ledgered ID is told, empty for any other
    ID."""
    if rule_id not in records:
        return ""
    record = records[rule_id]
    return (
        f"rule {rule_id} is retired: {record['reason']} "
        f"Replaced by {record['replacement']}."
    )


def refuse_unknown_rules(source: str, allowed: list[str]) -> None:
    """Stop when an allowed key is a ledgered rule ID, giving its reason, or is
    not a rule in the catalog."""
    records = ledger_records()
    catalog = catalog_rule_ids()
    for key in allowed:
        message = ledger_message(key, records)
        if message:
            stop(f"{source}.{key}: {message}")
        if key not in catalog:
            stop(f"{source}.{key}: not a check or a rule ID in the catalog")
