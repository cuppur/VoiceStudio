from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_quick_launcher_is_portable_and_prefers_packaged_app():
    source = (ROOT / "quick_launcher.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert tree is not None
    assert 'root / "dist" / "LocalVoiceStudio" / "LocalVoiceStudio.exe"' in source
    assert 'cwd=str(target.parent)' in source
    assert "C:\\Users\\" not in source


def test_build_scripts_apply_icon_and_generate_root_launcher():
    build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8-sig")
    quick = (ROOT / "scripts" / "build_quick_launcher.ps1").read_text(encoding="utf-8-sig")
    assert "assets\\voicestudio.ico" in build
    assert "build_quick_launcher.ps1" in build
    assert '--name "VoiceStudio-一键启动"' in quick
    assert "--onefile" in quick
    assert (ROOT / "assets" / "voicestudio.ico").is_file()
