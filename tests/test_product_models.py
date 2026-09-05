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
