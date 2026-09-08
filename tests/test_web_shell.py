"""Contract tests for the QtWebEngine HTML shell.

The shell renders the approved v4 prototype verbatim and exposes real local
state through the ``bridge`` object.  The rendered DOM is verified in a
subprocess (QtWebEngine cannot be torn down inside pytest); the bridge contract
is tested directly on :class:`StudioBridge`, which needs no browser.  Pixel
equality with the prototype is produced by ``scripts/capture_web_baseline.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web import shell as web_shell
from local_voice_studio.ui.web.bridge import StudioBridge

PROTOTYPE = Path(__file__).resolve().parents[1] / "VoiceStudio_Full_UI_Prototype_v4.html"


def _paths(root: Path) -> AppPaths:
    data = root / "data"
    return AppPaths(
        data_root=data,
        projects_root=root / "projects",
        runtime_root=data / "runtime",
        engine_root=data / "engines" / "GPT-SoVITS",
        models_root=data / "models",
        logs_root=data / "logs",
        database=data / "studio.sqlite3",
        cache_directory=data / "cache",
    )


def _project(tmp_path: Path, *, profile: bool = True, cover: bool = True):
    paths = _paths(tmp_path)
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("测试工程")
    if profile:
        store.save_profile(project, VoiceProfile(name="测试声音", consent_confirmed=True))
    if cover:
        CoverProject.create(project, title="落日信号").save()
    return paths, store, project


def test_shell_assets_are_a_verbatim_prototype_copy():
    """index.html must differ from the prototype only by the adapter script tag."""
    if not PROTOTYPE.is_file():
        pytest.skip("prototype file is not part of this checkout")
    source = PROTOTYPE.read_text(encoding="utf-8")
    asset = (web_shell.ASSETS / "index.html").read_text(encoding="utf-8")
    expected = source.replace(
        "</script>\n</body></html>",
        "</script>\n<script src=\"studio-bridge.js\"></script>\n<script src=\"studio-pages.js\"></script>\n</body></html>",
        1,
    )
    assert asset == expected
    assert (web_shell.ASSETS / "studio-bridge.js").is_file()
    assert (web_shell.ASSETS / "studio-pages.js").is_file()


def test_shell_renders_prototype_pages_with_real_data():
    """Boot the real shell out of process and assert DOM, metrics and real data.

    QtWebEngine must not run inside pytest: its child processes keep the
    interpreter alive after the session ends, so the probe runs in a subprocess
    and force-exits (see ``tests/helpers/web_shell_probe.py``).
    """
    probe_script = Path(__file__).parent / "helpers" / "web_shell_probe.py"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    result = subprocess.run(
        [sys.executable, str(probe_script)],
        capture_output=True, text=True, timeout=300, env=environment, check=False,
    )
    line = next((item for item in result.stdout.splitlines() if item.startswith("VS_PROBE ")), "")
    assert line, f"probe produced no result\nstdout:\n{result.stdout}\nstderr:\n{result.stderr[-2000:]}"
    payload = json.loads(line[len("VS_PROBE "):])
    probe = json.loads(payload["probe"])
    # prototype structure
    assert probe["pages"] == 8
    assert probe["nav"] == 6
    assert probe["brand"] == "VoiceStudio"
    # the engine viewport must match the 1440x900 window the baseline uses
    assert probe["viewport"] == [1440, 900]
    assert probe["topbarRect"] == 1440
    # prototype layout metrics
    assert probe["topbar"] == 78
    assert probe["transport"] == 68
    assert probe["columns"] == 3
    assert probe["radius"] == "17px"
    assert probe["background"] == "rgb(246, 243, 239)"
    assert probe["titleSize"] == "22px"
    assert probe["titleWeight"] == "820"
    # library column keeps the prototype's 242px / 320px side columns
    assert probe["coverRect"][2] == 1404  # 1440 - 2 * 18px page padding
    assert probe["cardRect"][2] == 242
    # real local data through the bridge
    assert probe["adapter"] is True
    assert probe["error"] == ""
    assert probe["songs"] == 1
    assert probe["count"] == "1 首"
    assert probe["hero"] == "落日信号"
    assert probe["voices"] == 1
    assert probe["tts"] == 1
    assert probe["detail"] == "测试声音"
    assert str(payload["project"]) in str(probe["outputPath"])
    # local audio URLs keep the drive colon and encode Chinese/spaces
    assert probe["fileUrl"].startswith("file:///C:/Users/")
    assert "%20" in probe["fileUrl"]
    assert "C%3A" not in probe["fileUrl"]
    # prototype interactions still work with the adapter attached
    assert probe["ttsVisible"] is True and probe["coverHidden"] is True
    assert probe["coverVisible"] is True
    assert probe["drawerOpen"] is True and probe["drawerClosed"] is True
    assert probe["advancedOpen"] is True
    assert probe["modalOpen"] is True and probe["modalClosed"] is True
    assert probe["takeActive"] is True
    assert probe["presetActive"] is True


def test_bridge_reports_unwired_actions_explicitly(tmp_path: Path):
    _paths, store, project = _project(tmp_path)
    bridge = StudioBridge(_paths, store, project)
    reply = json.loads(bridge.invoke("training.start", "{}"))
    assert reply["ok"] is False
    assert "尚未接入" in reply["message"]
    unknown = json.loads(bridge.invoke("does.not.exist", "{}"))
    assert unknown["ok"] is False


def test_bridge_imports_a_real_audio_file_into_the_project(tmp_path: Path):
    paths, store, project = _project(tmp_path, profile=False, cover=False)
    bridge = StudioBridge(paths, store, project)
    source = paths.projects_root / "sample.wav"
    with wave.open(str(source), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    reply = json.loads(bridge.invoke("song.import", json.dumps({"path": str(source)})))
    assert reply["ok"] is True
    covers = CoverProject.list(project)
    assert len(covers) == 1
    assert Path(covers[0].source_path or (covers[0].root / covers[0].source_relative_path)).is_file()
    assert json.loads(bridge.bootstrap())["data"]["songs"][0]["title"] == "sample"


def test_bridge_rejects_page_supplied_import_outside_managed_roots(tmp_path: Path):
    """A page-supplied import path must not read arbitrary local files."""
    paths, store, project = _project(tmp_path, profile=False, cover=False)
    bridge = StudioBridge(paths, store, project)
    outside = tmp_path / "outside.wav"
    with wave.open(str(outside), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    reply = json.loads(bridge.invoke("song.import", json.dumps({"path": str(outside)})))
    assert reply["ok"] is False
    assert "管理的目录" in reply["message"]
    assert CoverProject.list(project) == []


def test_bridge_rejects_parent_escape_import(tmp_path: Path):
    """`..` inside a page-supplied path must not escape the managed roots."""
    paths, store, project = _project(tmp_path, profile=False, cover=False)
    bridge = StudioBridge(paths, store, project)
    outside = tmp_path / "outside.wav"
    with wave.open(str(outside), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    escaped = paths.projects_root / ".." / "outside.wav"
    reply = json.loads(bridge.invoke("song.import", json.dumps({"path": str(escaped)})))
    assert reply["ok"] is False
    assert "管理的目录" in reply["message"]
    assert CoverProject.list(project) == []


def test_bridge_rejects_page_supplied_root_override(tmp_path: Path):
    """paths.overrides must stay behind the native directory picker."""
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    evil = {"projects": str(tmp_path / "evil")}
    reply = json.loads(bridge.invoke("settings.set", json.dumps({"key": "paths.overrides", "value": evil})))
    assert reply["ok"] is False
    assert not (store.get_setting("paths.overrides", {}) or {}).get("projects")


def test_bridge_rejects_paths_namespace_anywhere_in_setting_key(tmp_path: Path):
    """`ui.paths.overrides` must not act as an alias for the redirect key."""
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    for key in ("paths.overrides", "ui.paths.overrides", "generation.paths.overrides"):
        reply = json.loads(bridge.invoke("settings.set", json.dumps({"key": key, "value": {"projects": "C:/evil"}})))
        assert reply["ok"] is False, key
    assert not (store.get_setting("paths.overrides", {}) or {}).get("projects")
    assert store.get_setting("ui.paths.overrides") is None
    allowed = json.loads(bridge.invoke("settings.set", json.dumps({"key": "ui.theme", "value": "dark"})))
    assert allowed["ok"] is True
    assert store.get_setting("ui.theme") == "dark"


def test_bridge_save_ignores_unwritable_settings(tmp_path: Path):
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    values = {"paths.overrides": {"projects": str(tmp_path / "evil")}, "ui.theme": "dark"}
    reply = json.loads(bridge.invoke("settings.save", json.dumps({"values": values})))
    assert reply["ok"] is True
    assert store.get_setting("ui.theme") == "dark"
    assert not (store.get_setting("paths.overrides", {}) or {}).get("projects")


def test_bridge_rejects_unsupported_audio_suffix(tmp_path: Path):
    paths, store, project = _project(tmp_path, profile=False, cover=False)
    bridge = StudioBridge(paths, store, project)
    other = paths.projects_root / "notes.txt"
    other.write_text("not audio", encoding="utf-8")
    reply = json.loads(bridge.invoke("song.import", json.dumps({"path": str(other)})))
    assert reply["ok"] is False
    assert "不支持的音频格式" in reply["message"]


def test_bridge_bootstrap_reports_engine_and_settings(tmp_path: Path):
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    payload = json.loads(bridge.bootstrap())
    assert payload["ok"] is True
    data = payload["data"]
    assert set(data) >= {"app", "project", "projects", "songs", "voices", "tasks", "exports", "engine", "storage", "settings"}
    # The isolated temporary root has no installation manifest, so the engine
    # must not claim a verified install even when a private interpreter exists.
    assert data["engine"]["manifest_valid"] is False
    assert data["engine"]["python"]
    assert data["settings"]["theme"] in {"light", "dark"}
    assert data["storage"]["data_root"].startswith(str(tmp_path))


def test_bridge_rejects_paths_outside_known_projects(tmp_path: Path):
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    reply = json.loads(bridge.invoke("project.activate", json.dumps({"path": str(tmp_path / "missing")})))
    assert reply["ok"] is False
    assert "不是 VoiceStudio 工程" in reply["message"] or "超出" in reply["message"]


def test_bridge_rejects_project_directory_outside_projects_root(tmp_path: Path):
    """A directory that merely contains project.json must not become writable."""
    paths, store, project = _project(tmp_path)
    outside = tmp_path / "outside" / "fake-project"
    outside.mkdir(parents=True)
    (outside / "project.json").write_text("{}", encoding="utf-8")
    bridge = StudioBridge(paths, store, project)
    reply = json.loads(bridge.invoke("project.activate", json.dumps({"path": str(outside)})))
    assert reply["ok"] is False
    assert "超出" in reply["message"]
    assert not (outside / "covers").exists()


def test_bridge_rejects_file_actions_outside_managed_roots(tmp_path: Path):
    paths, store, project = _project(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("not managed", encoding="utf-8")
    bridge = StudioBridge(paths, store, project)
    for action in ("file.open", "file.reveal"):
        reply = json.loads(bridge.invoke(action, json.dumps({"path": str(outside)})))
        assert reply["ok"] is False, action
        assert "管理的目录" in reply["message"], action


def test_bridge_does_not_open_the_local_database(tmp_path: Path):
    """The app database lives in data_root, which the page must not open."""
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    reply = json.loads(bridge.invoke("file.open", json.dumps({"path": str(paths.database)})))
    assert reply["ok"] is False
    assert "管理的目录" in reply["message"]


def test_bridge_allows_revealing_project_files(tmp_path: Path, monkeypatch):
    import local_voice_studio.ui.web.bridge as bridge_module

    opened: list[str] = []

    class _FakeDesktop:
        @staticmethod
        def openUrl(url):  # noqa: N802 - mirrors the Qt API
            opened.append(url.toLocalFile())

    monkeypatch.setattr(bridge_module, "QDesktopServices", _FakeDesktop)
    paths, store, project = _project(tmp_path)
    bridge = StudioBridge(paths, store, project)
    song = json.loads(bridge.bootstrap())["data"]["songs"][0]
    reply = json.loads(bridge.invoke("file.reveal", json.dumps({"path": song["root"]})))
    assert reply["ok"] is True
    assert opened and Path(opened[0]).is_dir()


def test_bridge_switches_to_a_real_project(tmp_path: Path):
    paths, store, project = _project(tmp_path)
    second = store.create_project("第二个工程")
    bridge = StudioBridge(paths, store, project)
    reply = json.loads(bridge.invoke("project.activate", json.dumps({"path": str(second)})))
    assert reply["ok"] is True
    assert reply["data"]["project"]["name"] == "第二个工程"
    assert [item["name"] for item in reply["data"]["projects"] if item["active"]] == ["第二个工程"]
