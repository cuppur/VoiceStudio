from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "runtime-assets-v1.json"
CLI = ROOT / "src" / "local_voice_studio" / "lyrics_cli.py"
BOOTSTRAP = ROOT / "scripts" / "bootstrap_runtime.ps1"


def _lyrics_assets() -> list[dict]:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [asset for asset in data["assets"] if asset["id"].startswith("lyrics-")]


def test_lyrics_runtime_is_pinned():
    assets = _lyrics_assets()
    assert len(assets) == 10
    for asset in assets:
        assert asset["size"] > 0
        assert len(asset["sha256"]) == 64
        assert asset["license"] == "Apache-2.0"
        assert len(asset["source_revision"]) == 40
        assert asset["source"].startswith("https://modelscope.cn/models/")
        assert all(url.startswith("https://") for url in asset["urls"])
        assert "resolve/main" not in asset["urls"][0]
        assert "resolve/master" not in asset["urls"][0]


def test_lyrics_cli_uses_local_model_path():
    source = CLI.read_text(encoding="utf-8")
    assert 'parser.add_argument("--model-dir", required=True)' in source
    assert 'parser.add_argument("--vad-dir", required=True)' in source
    assert 'model=str(model_dir)' in source
    assert 'vad_model=str(vad_dir)' in source
    assert "_verify_model_directory(model_dir, \"SenseVoiceSmall\")" in source
    assert "_verify_model_directory(vad_dir, \"FSMN-VAD\")" in source
    assert "hashlib.sha256" in source


def test_lyrics_cli_does_not_implicit_download():
    source = CLI.read_text(encoding="utf-8")
    assert 'model="iic/SenseVoiceSmall"' not in source
    assert 'vad_model="fsmn-vad"' not in source
    assert "disable_update=True" in source
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8-sig")
    assert "Install-LyricsRuntime" in bootstrap
    assert "Invoke-PinnedAssetDownload -Id $id" in bootstrap
    assert "[switch]$LyricsOnly" in bootstrap
