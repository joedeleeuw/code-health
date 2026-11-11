"""Per-rule hit rates of the Rust preset over the corpus in corpus.json. Internal measurement, not shipped.

Usage: python3 corpus_rates.py CLONE_ROOT [CLONE_ROOT ...]
A repository is read from the first root that holds a folder of its name.
"""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent
CLONES = json.loads((REPOSITORY / "corpus.json").read_text())["clones"]
roots = [Path(root) for root in sys.argv[1:]]
hits: Counter = Counter()
files: Counter = Counter()
for clone in CLONES:
    if "rust" not in clone["languages"]:
        continue
    found = [root / clone["name"] for root in roots if (root / clone["name"]).is_dir()]
    if not found:
        raise SystemExit(f"{clone['name']}: not under any given clone root")
    listed = subprocess.run(
        ["git", "ls-files", "*.rs"],
        cwd=found[0],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    files[clone["role"]] += len(listed.splitlines())
    for config in ("sgconfig.yml", "sgconfig-off.yml"):
        scanned = subprocess.run(
            [
                "ast-grep",
                "scan",
                "-c",
                str(REPOSITORY / "presets" / "rust" / config),
                "--json=stream",
                ".",
            ],
            cwd=found[0],
            capture_output=True,
            text=True,
            check=False,
        )
        if scanned.returncode not in (0, 1):
            raise SystemExit(f"{clone['name']}: ast-grep exited {scanned.returncode}")
        for row in filter(None, scanned.stdout.split("\n")):
            hits[clone["role"], json.loads(row)["ruleId"]] += 1
roles = sorted(files)
print("rule".ljust(34) + "".join(f"{role + ' /1k files':>28}" for role in roles))
for rule in sorted({rule for _, rule in hits}):
    print(
        rule.ljust(34)
        + "".join(f"{hits[role, rule] / files[role] * 1000:28.1f}" for role in roles)
    )
