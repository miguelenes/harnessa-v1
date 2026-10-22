from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

PACKAGES = (
    "api",
    "web",
    "ui-react",
    "cloud-portal",
    "ui-svelte",
    "marketing",
)
_GENERATED_PARTS = {".next", ".svelte-kit", "build", "coverage", "dist", "node_modules"}


def package_for(path: str) -> str | None:
    normalized = path.replace("\\", "/")
    if _GENERATED_PARTS.intersection(normalized.split("/")):
        return None
    if "/apps/api/harnessa/" in f"/{normalized}":
        return "api"
    if "/apps/web/" in f"/{normalized}":
        return "web"
    if "/packages/ui-react/src/" in f"/{normalized}":
        return "ui-react"
    if "/apps/cloud-portal/src/" in f"/{normalized}":
        return "cloud-portal"
    if "/packages/ui-svelte/src/" in f"/{normalized}":
        return "ui-svelte"
    if "/apps/marketing/src/" in f"/{normalized}":
        return "marketing"
    return None


def marketing_is_static_only() -> bool:
    source_root = Path("apps/marketing/src")
    sources = [path for path in source_root.rglob("*") if path.is_file()]
    return bool(sources) and all(path.suffix in {".astro", ".css"} for path in sources)


def read_python(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        raise TypeError(f"{path} does not contain a coverage.py 'files' mapping")
    return files


def read_javascript(paths: list[Path]) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError(f"{path} does not contain an Istanbul coverage mapping")
        files.update(data)
    return files


def add_python_coverage(
    files: dict[str, dict[str, Any]],
    totals: dict[str, dict[str, int]],
    source_lines: dict[str, dict[str, dict[int, bool]]],
) -> None:
    for file_path, details in files.items():
        package = package_for(file_path)
        if package != "api":
            continue
        executed = {int(line) for line in details.get("executed_lines", [])}
        missing = {int(line) for line in details.get("missing_lines", [])}
        lines = source_lines[package].setdefault(file_path, {})
        lines.update({line: True for line in executed})
        lines.update({line: False for line in missing if line not in lines})
        summary = details.get("summary", {})
        totals[package]["statements"] += int(summary.get("num_statements", len(executed) + len(missing)))
        totals[package]["covered_statements"] += int(summary.get("covered_lines", len(executed)))


def add_javascript_coverage(
    files: dict[str, dict[str, Any]],
    totals: dict[str, dict[str, int]],
    source_lines: dict[str, dict[str, dict[int, bool]]],
) -> None:
    for file_path, details in files.items():
        package = package_for(file_path)
        if package is None:
            continue
        statements = details.get("s", {})
        statement_map = details.get("statementMap", {})
        totals[package]["statements"] += len(statements)
        totals[package]["covered_statements"] += sum(value > 0 for value in statements.values())
        lines = source_lines[package].setdefault(file_path, {})
        for statement_id, span in statement_map.items():
            start = span.get("start", {}).get("line")
            if not isinstance(start, int):
                continue
            hit = statements.get(statement_id, 0) > 0
            lines[start] = lines.get(start, False) or hit
        if not statement_map:
            functions = details.get("f", {})
            for function_id, function in details.get("fnMap", {}).items():
                line = function.get("line") or function.get("loc", {}).get("start", {}).get("line")
                if isinstance(line, int):
                    lines[line] = lines.get(line, False) or functions.get(function_id, 0) > 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate first-party Python and JavaScript coverage.")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--javascript", type=Path, nargs="+", required=True)
    parser.add_argument("--minimum", type=float, default=85.0)
    args = parser.parse_args()

    totals = defaultdict(lambda: {"statements": 0, "covered_statements": 0})
    source_lines: dict[str, dict[str, dict[int, bool]]] = defaultdict(dict)
    add_python_coverage(read_python(args.python), totals, source_lines)
    add_javascript_coverage(read_javascript(args.javascript), totals, source_lines)

    failing: list[str] = []
    all_statement_count = 0
    all_covered_statements = 0
    all_line_count = 0
    all_covered_lines = 0
    print(f"First-party coverage by package (line and statement thresholds: {args.minimum:.1f}%)")
    print("package        lines            statements")
    for package in PACKAGES:
        line_map = source_lines[package]
        line_count = sum(len(lines) for lines in line_map.values())
        covered_lines = sum(sum(lines.values()) for lines in line_map.values())
        statement_count = totals[package]["statements"]
        covered_statements = totals[package]["covered_statements"]
        if not line_count and not statement_count and package == "marketing" and marketing_is_static_only():
            print(f"{package:<14} static Astro/CSS markup; build artifact checked")
            continue
        line_pct = 100 * covered_lines / line_count if line_count else 0.0
        statement_pct = (
            100 * covered_statements / statement_count if statement_count else 0.0
        )
        print(
            f"{package:<14} {covered_lines:>5}/{line_count:<5} {line_pct:>6.2f}%"
            f"   {covered_statements:>5}/{statement_count:<5} {statement_pct:>6.2f}%"
        )
        if not line_count or not statement_count or line_pct < args.minimum or statement_pct < args.minimum:
            failing.append(package)
        all_line_count += line_count
        all_covered_lines += covered_lines
        all_statement_count += statement_count
        all_covered_statements += covered_statements

    line_pct = 100 * all_covered_lines / all_line_count if all_line_count else 0.0
    statement_pct = (
        100 * all_covered_statements / all_statement_count if all_statement_count else 0.0
    )
    print(
        f"{'aggregate':<14} {all_covered_lines:>5}/{all_line_count:<5} {line_pct:>6.2f}%"
        f"   {all_covered_statements:>5}/{all_statement_count:<5} {statement_pct:>6.2f}%"
    )
    if line_pct < args.minimum or statement_pct < args.minimum:
        failing.append("aggregate")
    if failing:
        print("Coverage gate failed: " + ", ".join(failing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
