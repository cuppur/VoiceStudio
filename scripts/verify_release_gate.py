"""Verify the signed-release approval artifact, refusing incomplete reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

REQUIRED_GATES = tuple("ABCDEFGHIJKL")


def verify(report_path: Path, commit: str) -> None:
    if not report_path.is_file():
        raise ValueError(f"Gate report is required: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("commit") != commit:
        raise ValueError("Gate report commit does not match the checked-out commit")
    gates = report.get("gates")
    if not isinstance(gates, dict) or any(gates.get(g) != "PASS" for g in REQUIRED_GATES):
        raise ValueError("All required gates A-L must be explicitly PASS")
    if report.get("release_status") != "APPROVED":
        raise ValueError("Gate report is not approved for release")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    verify(args.report, args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
