"""Historical cover labels must follow each mix's recorded voice and vocal."""
from __future__ import annotations

import hashlib
from pathlib import Path

from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.singing.models import SingingModelVersion
from local_voice_studio.storage import StudioStore
from local_voice_studio.ui.web.data import StudioSnapshot
from local_voice_studio.ui.web.services.cover import CoverService
from local_voice_studio.ui.web.takes import take_rows


def _fixture(root: Path):
    data = root / "data"
    paths = AppPaths(data, root / "projects", data / "runtime", data / "engine",
                     root / "models", data / "logs", data / "studio.sqlite3", root / "cache")
    paths.ensure()
    store = StudioStore(paths)
    project = store.create_project("版本归属测试")
    first = VoiceProfile("小岚", True, id="voice-one")
    second = VoiceProfile("女声 A", True, id="voice-two")
    first.singing_models = [
        SingingModelVersion(id="model-old", profile_id=first.id, checkpoint_relative_path="models/小岚-v1.pth"),
        SingingModelVersion(id="model-new", profile_id=first.id, checkpoint_relative_path="models/小岚-v2.pth"),
    ]
    first.active_singing_model_id = "model-new"
    second.singing_models = [SingingModelVersion(id="model-female", profile_id=second.id,
                                                checkpoint_relative_path="models/女声-v1.pth")]
    for profile in (first, second):
        store.save_profile(project, profile)
    cover = CoverProject.create(project, title="同一首歌")
    return paths, store, project, cover, [first, second]


def _asset(cover, identifier, role, *, sources=(), model="", metadata=None, missing=False):
    relative_path = f"generated/{identifier}.wav"
    path = cover.root / relative_path
    if not missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(identifier.encode())
    asset = CoverAsset(identifier, role, relative_path,
                       "" if missing else hashlib.sha256(path.read_bytes()).hexdigest(),
                       "ai_generated", "test", model_id=model,
                       source_asset_ids=list(sources), metadata=metadata or {})
    cover.add_asset(asset, save=False)
    return asset


def test_versions_keep_historical_voice_model_pitch_and_per_voice_numbering(tmp_path):
    _paths, _store, _project, cover, profiles = _fixture(tmp_path)
    _asset(cover, "vocal-old", "ai_vocal", model="model-old",
           metadata={"inference_settings": {"transpose": -3, "index_rate": 0.6, "protect": 0.4, "pitch_smoothing": "light"}})
    first = _asset(cover, "mix-one", "final_mix", sources=["vocal-old"], model="model-old",
                   metadata={"profile_id": "voice-one", "settings": {"vocal_gain": 0.8}})
    _asset(cover, "vocal-female", "ai_vocal", model="model-female", metadata={"inference_settings": {"transpose": 2}})
    _asset(cover, "mix-two", "final_mix", sources=["vocal-female"], model="model-female",
           metadata={"profile_id": "voice-two"})
    _asset(cover, "vocal-new", "ai_vocal", model="model-new", metadata={"inference_settings": {"transpose": 0}})
    _asset(cover, "mix-three", "final_mix", sources=["vocal-new"], model="model-new",
           metadata={"profile_id": "voice-one"})
    # Selecting an older version must not reorder history or change its labels.
    cover.active_take_id = first.id
    before = cover.to_dict()
    rows = take_rows(cover, profiles)
    assert [row["id"] for row in rows] == ["mix-one", "mix-two", "mix-three"]
    assert [row["voice_name"] for row in rows] == ["小岚", "女声 A", "小岚"]
    assert [row["version_label"] for row in rows] == ["V01", "V01", "V02"]
    assert [row["pitch_shift"] for row in rows] == [-3, 2, 0]
    assert [row["model_name"] for row in rows] == ["小岚-v1", "女声-v1", "小岚-v2"]
    assert rows[0]["settings"] == {"vocal_gain": 0.8}
    assert rows[0]["inference_settings"] == {"transpose": -3, "index_rate": 0.6, "protect": 0.4, "pitch_smoothing": "light"}
    assert rows[0]["model_id"] == first.model_id
    assert rows[0]["source_asset_ids"] == first.source_asset_ids
    assert rows[0]["sha256"] == first.sha256 and rows[0]["created_at"] == first.created_at
    assert all(row["exists"] for row in rows)
    assert cover.to_dict() == before


def test_legacy_lineage_fallback_and_missing_provenance_stay_honest(tmp_path):
    _paths, _store, _project, cover, profiles = _fixture(tmp_path)
    _asset(cover, "vocal-history", "ai_vocal", model="model-old",
           metadata={"inference_settings": {"transpose": -4}})
    _asset(cover, "vocal-cleaned", "ai_vocal", sources=["vocal-history"])
    _asset(cover, "mix-legacy", "final_mix", sources=["vocal-cleaned"], model="model-old")
    # A newer unrelated vocal cannot supply missing parameters for an old mix.
    _asset(cover, "vocal-unrelated", "ai_vocal", model="model-new",
           metadata={"inference_settings": {"transpose": 8}})
    _asset(cover, "mix-unknown", "final_mix", model="model-old",
           metadata={"profile_id": "deleted-voice"}, missing=True)
    _asset(cover, "mix-unlinked", "final_mix", missing=True)
    rows = take_rows(cover, profiles)
    assert rows[0]["voice_id"] == "voice-one" and rows[0]["pitch_shift"] == -4
    assert rows[0]["inference_settings"] == {"transpose": -4}
    assert rows[1]["voice_id"] == "deleted-voice"
    assert rows[1]["voice_name"] == "未登记声音"
    assert rows[1]["pitch_shift"] is None and rows[1]["exists"] is False
    assert rows[1]["inference_settings"] == {}
    assert rows[2]["voice_id"] == "" and rows[2]["voice_name"] == "未登记声音"
    assert rows[2]["model_name"] == "未登记模型" and rows[2]["pitch_shift"] is None


def test_snapshot_and_cover_state_share_metadata_and_batch_profiles_once(tmp_path, monkeypatch):
    paths, store, project, cover, _profiles = _fixture(tmp_path)
    _asset(cover, "vocal-one", "ai_vocal", model="model-old", metadata={"inference_settings": {"transpose": -2}})
    _asset(cover, "mix-one", "final_mix", model="model-old", sources=["vocal-one"],
           metadata={"profile_id": "voice-one"})
    _asset(cover, "mix-two", "final_mix", model="model-old", sources=["vocal-one"],
           metadata={"profile_id": "voice-one"})
    cover.active_take_id = "mix-one"
    cover.save()
    calls = []
    list_profiles = store.list_profiles
    monkeypatch.setattr(store, "list_profiles", lambda path: (calls.append(path), list_profiles(path))[1])
    snapshot = StudioSnapshot(paths, store, project)
    # Avoid runtime/device probing; the real songs and voices still render.
    for method in ("app", "project_state", "projects", "tasks", "exports", "generations", "engine", "storage", "settings"):
        monkeypatch.setattr(snapshot, method, lambda: {})
    song = snapshot.state()["songs"][0]
    assert len(calls) == 1
    assert song["active_take_id"] == "mix-one"
    service = CoverService(paths, store, project, None)
    monkeypatch.setattr(service, "engines", lambda: {})
    calls.clear()
    state = service.state(cover.id)
    assert len(calls) == 1
    assert state["takes"] == song["takes"]
    assert state["active_take_id"] == song["active_take_id"]
    assert state["final_asset_id"] == "mix-one"
    assert CoverProject.load(project, cover.id).active_take_id == "mix-one"
