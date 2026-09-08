from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "create_product_sbom.py"


def test_product_sbom_merges_environment_manifest_and_unknown_licenses(tmp_path):
    manifest = {
        "schema_version": 1,
        "manifest_version": "test",
        "engine": {"name": "GPT-SoVITS", "commit": "abc123"},
        "python": "3.11",
        "pytorch": "2.7.1+cu128",
        "torchaudio": "2.7.1+cu128",
        "assets": [{
            "id": "sensevoice-model", "version": "rev1", "destination": "models/sensevoice.bin",
            "sha256": "a" * 64, "urls": ["https://example.invalid/sensevoice.bin"]
        }],
        "installed_file_pins": [{
            "id": "ffmpeg", "path": "tools/ffmpeg.exe", "sha256": "b" * 64,
            "license": "LGPL-2.1-or-later"
        }],
    }
    manifest_path = tmp_path / "runtime.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "out"
    result = subprocess.run([
        sys.executable, str(SCRIPT), "--manifest", str(manifest_path),
        "--output-dir", str(output), "--project-root", str(tmp_path)
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    cdx = json.loads((output / "sbom.cdx.json").read_text(encoding="utf-8"))
    components = {item["name"]: item for item in cdx["components"]}
    assert "GPT-SoVITS" in components
    assert "sensevoice-model" in components
    assert components["sensevoice-model"]["hashes"][0]["content"] == "a" * 64
    assert components["sensevoice-model"]["licenses"][0]["expression"] == "NOASSERTION"
    assert "ffmpeg" in components
    spdx = (output / "sbom.spdx").read_text(encoding="utf-8")
    assert "SPDXVersion: SPDX-2.3" in spdx
    assert "Created:" in spdx
    assert "FilesAnalyzed: false" in spdx
    assert "PackageCopyrightText: NOASSERTION" in spdx
    assert "DESCRIBES" in spdx
    assert "PackageName: sensevoice-model" in spdx
    assert re.fullmatch(r"urn:uuid:[0-9a-f-]{36}", cdx["serialNumber"])

    sensevoice = components["sensevoice-model"]
    properties = {item["name"]: item["value"] for item in sensevoice["properties"]}
    assert properties["voicestudio:component-type"] == "model"
    assert properties["voicestudio:source-revision"] == "NOASSERTION"
    assert components["GPT-SoVITS"]["externalReferences"][0]["url"].startswith("https://github.com/")


def test_release_gate_is_fail_closed(tmp_path):
    from scripts.verify_release_gate import verify

    report = tmp_path / "gate.json"
    report.write_text(json.dumps({"commit": "abc", "release_status": "APPROVED", "gates": {}}), encoding="utf-8")
    try:
        verify(report, "abc")
    except ValueError as error:
        assert "A-L" in str(error)
    else:
        raise AssertionError("incomplete gate report was accepted")


def test_installer_deletes_only_two_default_data_roots_and_warns_custom_paths():
    source = (ROOT / "installer/LocalVoiceStudio.iss").read_text(encoding="utf-8")
    assert "{localappdata}\\LocalVoiceStudio" in source
    assert "{userprofile}\\Documents\\LocalVoiceStudio" in source
    assert "自定义保存到其他位置的工程不会自动删除" in source
    assert "是否删除默认 VoiceStudio 用户数据" in source
    assert "全部用户数据" not in source
