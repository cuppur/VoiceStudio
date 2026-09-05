from local_voice_studio.product_models import CacheArtifact, SongProject, Take


def test_song_project_round_trips_multiple_takes_and_cache_deduplication():
    project = SongProject(name="demo", source_asset_id="source-1")
    first = Take(project_id=project.id, voice_profile_id="voice-a", name="Take 01")
    second = Take(project_id=project.id, voice_profile_id="voice-b", name="Take 02")
    project.add_take(first)
    project.add_take(second)
    artifact = CacheArtifact(operation="separation", cache_key="k1", asset_id="a1", source_sha256="a" * 64, engine_version="sep-1")
    assert project.register_cache(artifact) is artifact
    duplicate = CacheArtifact(operation="separation", cache_key="k1", asset_id="a2", source_sha256="a" * 64, engine_version="sep-1")
    assert project.register_cache(duplicate) is artifact
    restored = SongProject.from_dict(project.to_dict())
    assert [item.name for item in restored.takes] == ["Take 01", "Take 02"]
    assert restored.active_take_id == second.id
    assert restored.cache_artifacts[0].asset_id == "a1"


def test_song_project_rejects_take_from_another_project():
    project = SongProject(name="demo", source_asset_id="source-1")
    other = Take(project_id="other", voice_profile_id="voice-a")
    try:
        project.add_take(other)
    except ValueError as exc:
        assert "不属于" in str(exc)
    else:
        raise AssertionError("foreign take was accepted")
from local_voice_studio.product_models import Take


def test_store_round_trips_product_project(tmp_path):
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    paths = AppPaths(tmp_path / "home", tmp_path / "projects", tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "database.sqlite3")
    store = StudioStore(paths)
    project_path = store.create_project("产品工程")
    product = store.load_song_project(project_path)
    take = Take(project_id=product.id, voice_profile_id="voice-1", name="Take 01")
    product.add_take(take)
    store.save_song_project(project_path, product)
    restored = store.load_song_project(project_path)
    assert restored.id == product.id
    assert restored.active_take_id == take.id
    assert restored.takes[0].voice_profile_id == "voice-1"
    assert store.load_project(project_path)["schema_version"] >= 4

from local_voice_studio.models import VoiceProfile
from local_voice_studio.product_models import voice_capabilities


def test_voice_capabilities_never_fakes_model_readiness():
    profile = VoiceProfile(name="empty", consent_confirmed=True)
    status = voice_capabilities(profile)
    assert status.tts == "unavailable"
    assert status.singing_conversion == "unavailable"
    assert not status.can("tts")
    assert "tts" in status.reasons


def test_voice_capabilities_exposes_verified_singing_model():
    from local_voice_studio.singing.models import SingingModelVersion
    profile = VoiceProfile(name="singing", consent_confirmed=True)
    profile.active_gpt_checkpoint = "gpt.pth"
    profile.active_sovits_checkpoint = "sovits.pth"
    profile.singing_models = [SingingModelVersion(profile_id=profile.id, trust_status="verified")]
    status = voice_capabilities(profile)
    assert status.tts == "ready"
    assert status.singing_conversion == "ready"

def test_legacy_model_versions_share_product_view():
    from local_voice_studio.models import ModelVersion, VoiceProfile
    from local_voice_studio.singing.models import SingingModelVersion
    from local_voice_studio.product_models import model_version_records
    profile = VoiceProfile(name="mixed", consent_confirmed=True)
    profile.model_versions = [ModelVersion(id="tts-1", sovits_checkpoint="sovits.pth", trust_status="verified")]
    profile.singing_models = [SingingModelVersion(id="sing-1", profile_id=profile.id, engine="rvc", trust_status="verified")]
    records = model_version_records(profile)
    assert {item.kind for item in records} == {"tts", "singing"}
    assert {item.id for item in records} == {"tts-1", "sing-1"}


def test_take_variant_and_ab_compare():
    project = SongProject("song", "source")
    base = Take(project.id, "voice-a", parameters={"pitch": 0, "index": "a"}, asset_ids=["a1"])
    project.add_take(base)
    variant = project.create_take_variant(base.id, parameters={"pitch": 2}, name="Take B")
    variant.asset_ids = ["b1"]
    comparison = project.compare_takes(base.id, variant.id)
    assert comparison["same_voice"]
    assert comparison["parameter_changes"]["pitch"] == (0, 2)
    assert comparison["asset_changes"] == {"a_only": ["a1"], "b_only": ["b1"]}
