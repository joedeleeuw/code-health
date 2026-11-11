"""The ratchet: a value over its limit needs a pin, and a pin is a ceiling that
only goes down.

Size limits, docstring code and restated shapes (clone_pairs, one per shape)
take their limit from the config. Rule matches, dead paths, dated
docstrings, shadowing objects, single-call hops,
incomplete hop, shape and import analysis, import cycles and the seam categories
have limit 0.
Incomplete analysis is counted per file in its own category, so a pin on the
known restatements or cycles never covers an unknown. Unused code is a set:
every item outside the pinned set fails.
"""

CONFIGURED = ("file_lines", "complexity", "nesting", "params", "docstring_code")
INCOMPLETE = (
    "incomplete_hop_analysis",
    "incomplete_shape_analysis",
    "incomplete_import_analysis",
)
ZERO = (
    "dated_docstrings",
    "ast_grep",
    "nix_lints",
    "dead_paths",
    "shadow_key_prose",
    "single_call_hops",
    "unresolved_imports",
    *INCOMPLETE,
    "single_adapter_seams",
    "test_only_seams",
)
COUNTS = {"clone_pairs": None, "cycles": 0}
CATEGORIES = (*CONFIGURED, *ZERO, *COUNTS, "unused")


def ratchet_items(measured: dict, limit: int, old: dict, baseline: bool):
    """Pins for one per-item category in key order, so a rewritten config changes
    only the pins whose values moved, and its failing keys."""
    pins, failures = {}, {}
    for key, count in sorted(measured.items()):
        cap = old.get(key)
        if count <= limit:
            continue
        if baseline or (cap is not None and count <= cap):
            pins[key] = count
            continue
        over = f"limit {limit}" if cap is None else f"pin {cap}"
        failures[key] = f"{key} = {count} > {over}"
        if cap is not None:
            pins[key] = cap
    return pins, failures


def ratchet_count(count: int, limit: int, cap: int, baseline: bool):
    """The pin for one counted category, and whether it fails."""
    if baseline:
        return count, False
    return min(max(count, limit), cap), count > max(limit, cap)


def explained(failure: str, found: list[str]) -> str:
    return "\n    ".join([failure, *found])


def ratchet(measured: dict, findings: dict, config: dict, initialize: set[str]):
    """The new pins and the failures."""
    limits, old = config["limits"], config["pins"]
    pins, failures = {}, []
    for category in (*CONFIGURED, *ZERO):
        baseline = category in initialize
        limit = limits[category] if category in CONFIGURED else 0
        cap = {} if baseline else old[category]
        pins[category], failed = ratchet_items(measured[category], limit, cap, baseline)
        found = findings.get(category, {})
        failures += [
            f"{category}: {explained(failure, found.get(key, []))}"
            for key, failure in failed.items()
        ]
    for category, limit in COUNTS.items():
        baseline = category in initialize
        limit = limits[category] if limit is None else limit
        cap = 0 if baseline else old[category]
        count = measured[category]
        pins[category], failed = ratchet_count(count, limit, cap, baseline)
        if failed:
            found = findings.get(category, {}).get("", [])
            failure = f"{count} > pin {cap}"
            failures.append(f"{category}: {explained(failure, found)}")
    found = set(measured["unused"])
    known = found if "unused" in initialize else set(old["unused"])
    failures += [f"unused: {item}" for item in sorted(found - known)]
    pins["unused"] = sorted(found & known)
    return pins, failures
