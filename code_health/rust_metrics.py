"""Whole-workspace quantitative and structural metrics for Rust source files.

Measures file length, function complexity, control-flow nesting depth and
parameter counts, plus restated struct shapes, cloned function bodies and
single-call wrapper hops (code_health.rust_hops) across all measured Rust files
in the repository. A file whose syntax tree has errors is incomplete hop
analysis, and incomplete shape analysis when it hides a production struct's
fields, at each error.
"""

from collections import defaultdict
from pathlib import Path

from code_health import counted, keyed
from code_health.rust_crates import RustCrates
from code_health.rust_hops import RustHops
from code_health.rust_source import RustFile, RustFunction, RustStruct, parse_rust_file


def near_clone_findings(
    by_shape: dict[tuple[tuple[str, str], ...], list[RustStruct]],
) -> list[str]:
    near_shapes = [
        (
            fields_key,
            set(fields_key),
            sorted(group, key=lambda decl: (decl.path, decl.line, decl.name))[0],
        )
        for fields_key, group in sorted(
            by_shape.items(), key=lambda pair: (len(pair[0]), pair[0])
        )
        if len(fields_key) >= 4
    ]
    findings: list[str] = []
    for index, (fields_a, set_a, rep_a) in enumerate(near_shapes):
        for fields_b, set_b, rep_b in near_shapes[index + 1 :]:
            if len(fields_a) * 5 < len(fields_b) * 4:
                break
            shared = set_a & set_b
            if len(shared) * 5 < len(set_a | set_b) * 4:
                continue
            ordered = sorted(
                (rep_a, rep_b), key=lambda decl: (decl.path, decl.line, decl.name)
            )
            members = " ~ ".join(
                f"{decl.path}::{decl.name}:{decl.line}" for decl in ordered
            )
            field_names = ", ".join(sorted(field_name for field_name, _ in shared))
            findings.append(f"{members} ({field_names})")
    return findings


def clone_findings(files: list[RustFile]) -> list[str]:
    by_shape: dict[tuple[tuple[str, str], ...], list[RustStruct]] = defaultdict(list)
    by_body: dict[str, list[RustFunction]] = defaultdict(list)
    for parsed in files:
        for struct_decl in parsed.structs:
            by_shape[struct_decl.fields].append(struct_decl)
        for function in parsed.functions:
            if function.clone_key is not None:
                by_body[function.clone_key].append(function)
    findings: list[str] = []
    for fields_key, group in sorted(by_shape.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda decl: (decl.path, decl.line, decl.name))
        members = " = ".join(
            f"{decl.path}::{decl.name}:{decl.line}" for decl in ordered
        )
        field_names = ", ".join(field_name for field_name, _ in fields_key)
        findings.append(f"{members} ({field_names})")
    findings.extend(near_clone_findings(by_shape))
    function_findings: list[str] = []
    for group in by_body.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda decl: (decl.path, decl.line, decl.name))
        function_findings.append(
            " = ".join(
                f"{decl.path}::{decl.impl_type}::{decl.name}:{decl.line}"
                if decl.impl_type
                else f"{decl.path}::{decl.name}:{decl.line}"
                for decl in ordered
            )
        )
    findings.extend(sorted(function_findings))
    return findings


def quantitative_metrics(files: list[RustFile]) -> dict[str, dict[str, int]]:
    complexity_rows: list[tuple[str, int]] = []
    nesting_rows: list[tuple[str, int]] = []
    param_rows: list[tuple[str, int]] = []
    for parsed in files:
        for size in parsed.sizes:
            key = f"{parsed.path}::{size.name}"
            complexity_rows.append((key, size.complexity))
            nesting_rows.append((key, size.nesting))
            if size.arity is not None:
                param_rows.append((key, size.arity))
    return {
        "file_lines": {parsed.path: parsed.lines for parsed in files},
        "complexity": keyed(complexity_rows),
        "nesting": keyed(nesting_rows),
        "params": keyed(param_rows),
    }


UNREAD_FUNCTION = (
    "tree-sitter-rust cannot parse this function's code; "
    "its calls and the uses of names in it are not analysed"
)
UNREAD_STRUCT = "tree-sitter-rust cannot parse this struct; its fields are not compared"


def unparsed(lines_by_path: dict[str, list[int]], message: str) -> dict[str, list[str]]:
    return {
        path: [f"line {line}: {message}" for line in lines]
        for path, lines in lines_by_path.items()
        if lines
    }


def measure_rust(root: Path, files: list[str]) -> tuple[dict, dict]:
    crates = RustCrates(root)
    parsed_files = [
        parsed
        for path in files
        if (parsed := parse_rust_file(root, path, crates)) is not None
    ]
    clones = clone_findings(parsed_files)
    hops = RustHops(parsed_files, crates).findings()
    findings = {
        "clone_pairs": {"": clones},
        "single_call_hops": hops,
        "incomplete_hop_analysis": unparsed(
            {parsed.path: parsed.unread_functions for parsed in parsed_files},
            UNREAD_FUNCTION,
        ),
        "incomplete_shape_analysis": unparsed(
            {parsed.path: parsed.unread_structs for parsed in parsed_files},
            UNREAD_STRUCT,
        ),
    }
    counts = {
        **quantitative_metrics(parsed_files),
        "clone_pairs": len(clones),
        "single_call_hops": counted(hops),
        "incomplete_hop_analysis": counted(findings["incomplete_hop_analysis"]),
        "incomplete_shape_analysis": counted(findings["incomplete_shape_analysis"]),
    }
    return counts, findings
