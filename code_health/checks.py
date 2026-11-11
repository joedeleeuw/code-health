"""The checks, and which findings fail one.

Every check counts findings per key: a file, a function in a file, or a rule in
a file. A finding that spans files, a repeated shape or an import cycle, stands
in each of them. A count over its limit fails. The size checks take their limit from the
settings; every other check has limit 0. A finding fails unless the settings
allow that check in its file: a rule finding is allowed under its rule ID, any
other under the name of its check.
"""

import fnmatch

CONFIGURED = ("file_lines", "complexity", "nesting", "params", "docstring_code")
INCOMPLETE = (
    "incomplete_hop_analysis",
    "incomplete_shape_analysis",
    "incomplete_import_analysis",
)
RULES = ("ast_grep", "nix_lints")
PER_FUNCTION = ("complexity", "nesting", "params")
FOUND = (
    "dated_docstrings",
    "dead_paths",
    "shadow_key_prose",
    "single_call_hops",
    "unresolved_imports",
    *INCOMPLETE,
    "single_adapter_seams",
    "test_only_seams",
    "clone_pairs",
    "cycles",
    "unused",
    "unparsable",
)
CHECKS = (*CONFIGURED, *FOUND)


def failures(
    measured: dict, findings: dict, limits: dict, allowed: dict[str, list[str]]
) -> list[str]:
    failed = []
    for category in (*CONFIGURED, *RULES, *FOUND):
        limit = limits.get(category, 0)
        found = findings.get(category, {})
        for key, count in sorted(measured[category].items()):
            if count <= limit:
                continue
            if category in RULES:
                check, path = key.split("::", 1)
            elif category in PER_FUNCTION:
                check, path = category, key.rsplit("::", 1)[0]
            else:
                check, path = category, key
            if any(fnmatch.fnmatch(path, glob) for glob in allowed.get(check, [])):
                continue
            lines = [f"{key} = {count} > limit {limit}", *found.get(key, [])]
            failed.append(f"{category}: " + "\n    ".join(lines))
    return failed
