"""Generate the product SBOM from installed packages and pinned runtime assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOASSERTION = "NOASSERTION"
SPDX_ID = re.compile(r"^[A-Za-z0-9.-]+$")
KNOWN_SPDX = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "LGPL-2.1-or-later", "LGPL-3.0-or-later", "GPL-3.0-or-later", "MPL-2.0", "ISC", "CC0-1.0", "NOASSERTION"}


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_license(dist: metadata.Distribution) -> str:
    value = (dist.metadata.get("License") or "").strip()
    if value and value.upper() not in {"UNKNOWN", "NONE"}:
        return value
    classifiers = [x for x in dist.metadata.get_all("Classifier") or [] if x.startswith("License ::")]
    return classifiers[-1].rsplit("::", 1)[-1].strip() if classifiers else NOASSERTION


def license_info(value: str | None) -> tuple[str, str | None]:
    raw = (value or "").strip()
    if raw in KNOWN_SPDX:
        return raw, None
    if raw and "\n" not in raw and "\r" not in raw and re.fullmatch(r"[A-Za-z0-9.+() -]+", raw):
        return NOASSERTION, f"Original license metadata: {raw}"
    return NOASSERTION, (f"Original license metadata: {raw}" if raw else None)


def environment_components() -> list[dict]:
    result = []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata.get("Name") or "").lower()):
        name = dist.metadata.get("Name")
        if not name:
            continue
        purl = f"pkg:pypi/{name.lower().replace('_', '-')}@{dist.version}"
        expression, comment = license_info(package_license(dist))
        properties = [{"name": "voicestudio:component-type", "value": "python-package"}]
        if comment:
            properties.append({"name": "voicestudio:license-comment", "value": comment})
        result.append({
            "type": "library", "bom-ref": purl, "name": name, "version": dist.version,
            "purl": purl, "licenses": [{"expression": expression}], "properties": properties,
        })
    return result


def asset_type(asset: dict) -> str:
    value = f"{asset.get('id', '')} {asset.get('path', '')} {asset.get('destination', '')}".lower()
    if "source" in value and any(word in value for word in ("gpt", "rvc")):
        return "framework"
    if any(word in value for word in ("model", "weight", "hubert", "rmvpe", "roformer", "g2pw", "uvr")):
        return "machine-learning-model"
    if value.endswith((".exe", ".dll")) or "ffmpeg" in value:
        return "application"
    return "data"


def component_kind(asset: dict) -> str:
    value = f"{asset.get('id', '')} {asset.get('path', '')} {asset.get('destination', '')}".lower()
    if "source" in value and any(word in value for word in ("gpt", "rvc")):
        return "engine-source"
    if value.endswith((".exe", ".dll")) or "ffmpeg" in value or "miniforge" in value:
        return "runtime-binary"
    if any(word in value for word in ("model", "weight", "hubert", "rmvpe", "roformer", "g2pw", "uvr")):
        return "model"
    return "runtime-asset"


def manifest_components(manifest: dict, root: Path) -> list[dict]:
    result = []
    engine = manifest.get("engine") or {}
    if engine.get("name"):
        revision = engine.get("commit") or NOASSERTION
        source_url = engine.get("source_url") or "https://github.com/RVC-Boss/GPT-SoVITS"
        result.append({"type": "framework", "bom-ref": f"engine:{engine['name']}@{revision}",
                       "name": engine["name"], "version": revision,
                       "licenses": [{"license": {"name": NOASSERTION}}],
                       "externalReferences": [{"type": "vcs", "url": source_url}],
                       "properties": [{"name": "voicestudio:component-type", "value": "engine"},
                                       {"name": "voicestudio:source-revision", "value": revision}]})
    for key in ("python", "pytorch", "torchaudio"):
        if manifest.get(key):
            result.append({"type": "library", "bom-ref": f"runtime:{key}@{manifest[key]}",
                           "name": key, "version": str(manifest[key]),
                           "licenses": [{"license": {"name": NOASSERTION}}],
                           "properties": [{"name": "voicestudio:component-type", "value": "runtime"}]})
    seen = set()
    for asset in list(manifest.get("assets") or []) + list(manifest.get("installed_file_pins") or []):
        identity = asset.get("id") or asset.get("path") or asset.get("destination")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        path_value = asset.get("path") or asset.get("destination")
        resolved = root / path_value if path_value else None
        digest = asset.get("sha256") or (file_sha256(resolved) if resolved else None)
        expression, comment = license_info(asset.get("license"))
        component = {"type": asset_type(asset), "bom-ref": f"asset:{identity}", "name": identity,
                     "version": str(asset.get("version") or NOASSERTION),
                     "licenses": [{"expression": expression}],
                     "properties": [{"name": "voicestudio:path", "value": str(path_value or "")},
                                    {"name": "voicestudio:component-type", "value": component_kind(asset)},
                                    {"name": "voicestudio:source-revision", "value": str(asset.get("source_revision") or asset.get("source_model_revision") or NOASSERTION)}]}
        if comment:
            component["properties"].append({"name": "voicestudio:license-comment", "value": comment})
        if digest:
            component["hashes"] = [{"alg": "SHA-256", "content": digest}]
        if asset.get("urls"):
            component["externalReferences"] = [{"type": "distribution", "url": url} for url in asset["urls"]]
        result.append(component)
    return result


def write_spdx(components: list[dict], output: Path) -> None:
    lines = ["SPDXVersion: SPDX-2.3", "DataLicense: CC0-1.0", "SPDXID: SPDXRef-DOCUMENT",
             "DocumentName: LocalVoiceStudio-product", "DocumentNamespace: https://voicestudio.local/sbom/product-1.0.0",
             f"Created: {datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}",
             "Creator: Tool: LocalVoiceStudio create_product_sbom.py", ""]
    for index, item in enumerate(components, 1):
        license_name = (item["licenses"][0].get("expression") or item["licenses"][0].get("license", {}).get("name") or NOASSERTION)
        download = (item.get("externalReferences") or [{"url": NOASSERTION}])[0]["url"]
        lines += [f"##### Package: {item['name']}", f"PackageName: {item['name']}", f"SPDXID: SPDXRef-{index}",
                  f"PackageVersion: {item['version']}", f"PackageLicenseConcluded: {license_name}",
                  f"PackageLicenseDeclared: {license_name}", "FilesAnalyzed: false",
                  "PackageCopyrightText: NOASSERTION", f"PackageDownloadLocation: {download}"]
        comment = next((p["value"] for p in item.get("properties", []) if p["name"] == "voicestudio:license-comment"), None)
        if comment:
            lines.append(f"PackageComment: {comment.replace(chr(10), ' ').replace(chr(13), ' ')}")
        kind = next((p["value"] for p in item.get("properties", []) if p["name"] == "voicestudio:component-type"), None)
        revision = next((p["value"] for p in item.get("properties", []) if p["name"] == "voicestudio:source-revision"), None)
        if kind:
            lines.append(f"PackageComment: component-type={kind}")
        if revision:
            lines.append(f"PackageComment: source_revision={revision}")
        lines += [f"PackageChecksum: SHA256: {h['content']}" for h in item.get("hashes", [])] + [""]
        lines.append(f"Relationship: SPDXRef-DOCUMENT DESCRIBES SPDXRef-{index}")
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def create(manifest_path: Path, output_dir: Path, root: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_components = environment_components() + manifest_components(manifest, root)
    # CycloneDX 1.5 requires components to be unique. Keep the first
    # deterministic record for duplicate bom-ref values (for example when
    # the same distribution is visible through multiple Python metadata paths).
    unique: dict[str, dict] = {}
    for component in raw_components:
        unique.setdefault(str(component.get("bom-ref", "")), component)
    components = list(unique.values())
    components.sort(key=lambda c: (c["name"].lower(), c["version"], c["bom-ref"]))
    document = {"bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'https://voicestudio.local/sbom/product-1.0.0')}",
                "version": 1, "metadata": {"timestamp": datetime.now(timezone.utc).isoformat(),
                "tools": [{"vendor": "LocalVoiceStudio", "name": "create_product_sbom.py", "version": "1"}],
                "component": {"type": "application", "name": "LocalVoiceStudio", "version": "1.0.0"}},
                "components": components}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "sbom.cdx.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_spdx(components, output_dir / "sbom.spdx")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "manifests/runtime-assets-v1.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release-metadata")
    parser.add_argument("--project-root", type=Path, default=ROOT)
    args = parser.parse_args()
    create(args.manifest, args.output_dir, args.project_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
