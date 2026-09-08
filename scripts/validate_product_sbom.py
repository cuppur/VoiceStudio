"""Fail-closed validation for the generated product SBOMs."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def validate_cyclonedx(path: Path, schema: Path) -> None:
    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError("jsonschema is required for official CycloneDX schema validation") from exc
    document = json.loads(path.read_text(encoding="utf-8"))
    schema_data = json.loads(schema.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema_data).validate(document)
    if not re.fullmatch(r"urn:uuid:[0-9a-fA-F-]{36}", document.get("serialNumber", "")):
        raise ValueError("CycloneDX serialNumber is not a UUID URN")


def validate_spdx(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    required = ("SPDXVersion: SPDX-2.3", "DataLicense: CC0-1.0", "SPDXID: SPDXRef-DOCUMENT", "Created:")
    missing = [line for line in required if line not in text]
    if missing:
        raise ValueError(f"SPDX required fields missing: {missing}")
    packages = re.findall(r"^SPDXID: (SPDXRef-[^\s]+)$", text, re.MULTILINE)
    package_ids = [x for x in packages if x != "SPDXRef-DOCUMENT"]
    if not package_ids:
        raise ValueError("SPDX contains no packages")
    if text.count("FilesAnalyzed: false") != len(package_ids):
        raise ValueError("Every SPDX package must declare FilesAnalyzed: false")
    if text.count("PackageCopyrightText: NOASSERTION") != len(package_ids):
        raise ValueError("Every SPDX package must declare copyright text")
    for package_id in package_ids:
        if f"Relationship: SPDXRef-DOCUMENT DESCRIBES {package_id}" not in text:
            raise ValueError(f"Missing DESCRIBES relationship for {package_id}")
        if not re.search(rf"^PackageLicense(?:Concluded|Declared): (?:NOASSERTION|[A-Za-z0-9.+() -]+)$", text, re.MULTILINE):
            raise ValueError("SPDX package license is not a single-line SPDX expression")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cyclonedx", type=Path, required=True)
    parser.add_argument("--cyclonedx-schema", type=Path, required=True)
    parser.add_argument("--spdx", type=Path, required=True)
    args = parser.parse_args()
    validate_cyclonedx(args.cyclonedx, args.cyclonedx_schema)
    validate_spdx(args.spdx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
