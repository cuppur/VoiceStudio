from local_voice_studio.cover.exporting.manifest import ProvenanceManifestBuilder
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.singing.models import RVCInferenceSettings


def test_pitch_smoothing_keeps_protocol_and_cache_but_names_export_honestly():
    settings = RVCInferenceSettings.from_payload({"autotune": "medium"})
    assert settings.canonical()["autotune"] == "medium"
    assert settings.to_payload()["inference_settings"]["autotune"] == "medium"
    assert settings.provenance()["pitch_smoothing"] == "medium"
    assert settings.provenance()["filter_radius"] == 7
    assert "autotune" not in settings.provenance()


def test_export_lineage_preserves_model_input_hashes_and_legacy_settings(tmp_path):
    cover = CoverProject.create(tmp_path / "p")
    source = CoverAsset("source", "vocal", "stems/v.wav", "a" * 64, "separated", "uvr")
    ai = CoverAsset("ai", "ai_vocal", "generated/a.wav", "b" * 64, "ai_generated", "rvc_v2",
                    model_id="model", model_sha256="c" * 64, source_asset_ids=[source.id],
                    metadata={"inference_settings": {"autotune": "light", "filter_radius": 3}})
    final = CoverAsset("mix", "final_mix", "generated/m.wav", "d" * 64, "ai_generated", "voicestudio_mixer", source_asset_ids=[ai.id])
    cover.assets = [source, ai, final]
    chain = ProvenanceManifestBuilder.asset_lineage(cover, final)
    assert [item["id"] for item in chain] == ["ai", "source"]
    assert chain[0]["model_sha256"] == "c" * 64
    assert chain[1]["sha256"] == "a" * 64
    assert chain[0]["metadata"]["inference_settings"] == {"pitch_smoothing": "light", "filter_radius": 3}
    assert ai.metadata["inference_settings"]["autotune"] == "light"


def test_export_lineage_reports_missing_assets_and_stops_cycles(tmp_path):
    cover = CoverProject.create(tmp_path / "p")
    asset = CoverAsset("mix", "final_mix", "m.wav", "d" * 64, "ai_generated", "voicestudio_mixer", source_asset_ids=["mix", "missing"])
    cover.assets = [asset]
    assert ProvenanceManifestBuilder.asset_lineage(cover, asset) == [{"id": "missing", "status": "missing"}]
