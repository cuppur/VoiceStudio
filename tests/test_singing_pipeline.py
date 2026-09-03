import hashlib
import json
import threading
import wave
import sys
import threading
import time
from pathlib import Path

import pytest

from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.singing.pipeline import SingingPipeline
from local_voice_studio.singing.rvc import RVCEngine, RVCConfig


def test_rvc_subprocess_cancel_without_output(tmp_path):
    engine = RVCEngine(RVCConfig(tmp_path, Path(sys.executable), tmp_path, commit="test"))
    cancel = threading.Event(); result = {}
    def run():
        try:
            engine._run([sys.executable, "-S", "-c", "import time; time.sleep(30)"], tmp_path, cancel)
        except RuntimeError as exc:
            result["error"] = str(exc)
    worker = threading.Thread(target=run); worker.start()
    time.sleep(0.15); cancel.set(); worker.join(5)
    assert not worker.is_alive()
    assert "取消" in result["error"]
    assert engine.process is None


def test_rvc_preprocess_maps_product_sample_rate_to_integer_hz(tmp_path):
    commands = []
    def runner(args, **_kwargs): commands.append(args); return 0
    experiment = tmp_path / "exp"; experiment.mkdir(); (experiment / "model.pth").write_bytes(b"m"); (experiment / "model.index").write_bytes(b"i")
    for name in ("0_gt_wavs", "3_feature768", "2a_f0", "2b-f0nsf"): (experiment / name).mkdir()
    (experiment / "0_gt_wavs" / "clip.wav").write_bytes(b"w"); (experiment / "3_feature768" / "clip.npy").write_bytes(b"f"); (experiment / "2a_f0" / "clip.wav.npy").write_bytes(b"p"); (experiment / "2b-f0nsf" / "clip.wav.npy").write_bytes(b"n")
    config = tmp_path / "configs" / "v2" / "48k.json"; config.parent.mkdir(parents=True); config.write_text("{}", encoding="utf-8")
    engine = RVCEngine(RVCConfig(tmp_path, Path(sys.executable), tmp_path, commit="test"), runner=runner)
    engine.train({"experiment_dir": str(experiment), "dataset_dir": str(tmp_path), "sample_rate": "48k", "prepare_checkpoint": False})
    assert commands[0][2:5] == ["train.preprocess", str(tmp_path), "48000"]
    assert commands[1][2:] == ["train.dataset.extract_f0", "cuda", "1", "0", "0", str(experiment), "True"]
    assert commands[2][2:] == ["train.dataset.extract_hubert_feature", "cuda:0", "1", "0", "0", str(experiment), "v2", "True"]
    assert commands[3][commands[3].index("-sw") + 1] == "0"
    assert commands[4][2:5] == ["train.train_index", "voicestudio_exp", "v2"]
    assert commands[4][-2:] == [str(experiment), "4"]


class FakeSingingEngine:
    def train(self, payload, cancel=None):
        path = Path(payload["experiment_dir"]) / "model.pth"
        path.write_bytes(b"fake checkpoint")
        index = path.with_suffix(".index"); index.write_bytes(b"fake index")
        return [path, index]

    def verify_model(self, checkpoint, index):
        return checkpoint.is_file() and index is not None and index.is_file()

    def convert(self, payload, cancel=None):
        path = Path(payload["output_path"])
        input_path = Path(payload["input_path"])
        try:
            with wave.open(str(input_path), "rb") as source:
                rate, channels, width, frames = source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getnframes()
        except wave.Error:
            rate, channels, width, frames = 16000, 1, 2, 160
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(channels); stream.setsampwidth(width); stream.setframerate(rate); stream.writeframes(b"\x02\x00" * frames * channels)
        return path


class CancellingEngine(FakeSingingEngine):
    def train(self, payload, cancel=None):
        if cancel is not None:
            cancel.set()
        return super().train(payload, cancel=cancel)


class FailingVerificationEngine(FakeSingingEngine):
    def verify_model(self, checkpoint, index): return False


class ExplodingVerificationEngine(FakeSingingEngine):
    def verify_model(self, checkpoint, index): raise RuntimeError("verification crashed")


class PitchEngine(FakeSingingEngine):
    def analyze_pitch(self, path, cancel=None):
        median = 100.0 if Path(path).name == "vocals.wav" else 200.0
        return {"backend": "rmvpe", "version": "rvc-rmvpe-v1", "median_hz": median,
                "minimum_hz": median - 10, "maximum_hz": median + 10, "voiced_frames": 20}


def _fixture(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cover = CoverProject.create(project, title="song")
    source = tmp_path / "original.wav"
    source.write_bytes(b"RIFForiginal")
    cover.copy_source(source)
    vocal = cover.root / "stems" / "vocals.wav"
    with wave.open(str(vocal), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(100); stream.writeframes(b"\x01\x00" * 500)
    cover.set_stem("vocal", vocal)
    cover.attest_rights()
    training = project / "raw" / "profile" / "training.wav"; training.parent.mkdir(parents=True)
    with wave.open(str(training), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(100); stream.writeframes(b"\x01\x00" * 18000)
    training_sha = hashlib.sha256(training.read_bytes()).hexdigest()
    (project / "project.json").write_text(json.dumps({"voice_profiles": [{
        "id": "profile", "name": "Authorized", "consent_confirmed": True,
        "consent_confirmed_at": "now", "consent_record": "test", "singing_models": []
    }], "source_assets": [{"id": "asset", "profile_id": "profile", "project_path": str(training), "sha256": training_sha, "duration_seconds": 180.0, "sample_rate": 100, "channels": 1, "codec": "pcm", "enabled": True, "duplicate_of": "", "quality_flags": []}]}), encoding="utf-8")
    return project, cover


def test_train_and_convert_registers_ai_asset(tmp_path):
    project, cover = _fixture(tmp_path)
    pipeline = SingingPipeline(FakeSingingEngine(), projects_root=tmp_path)
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    result = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id})
    restored = CoverProject.load(project, cover.id)
    asset = restored.get_asset(role="ai_vocal")
    assert model["profile_id"] == "profile"
    assert result["content_origin"] == "ai_generated"
    assert asset and asset.content_origin == "ai_generated" and Path(result["output_path"]).is_file()


def test_conversion_requires_song_rights(tmp_path):
    project, cover = _fixture(tmp_path)
    pipeline = SingingPipeline(FakeSingingEngine(), projects_root=tmp_path)
    pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    cover.attest_rights(False)
    with pytest.raises(ValueError, match="权利"):
        pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id})


def test_conversion_rejects_vocal_not_marked_as_separated(tmp_path):
    project, cover = _fixture(tmp_path)
    pipeline = SingingPipeline(FakeSingingEngine(), projects_root=tmp_path)
    pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    restored = CoverProject.load(project, cover.id)
    restored.get_asset(role="vocal").content_origin = "original"
    restored.save()
    with pytest.raises(ValueError, match="已分离"):
        pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id})


def test_training_rejects_unconsented_profile(tmp_path):
    project, _ = _fixture(tmp_path)
    manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
    manifest["voice_profiles"][0]["consent_confirmed"] = False
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="授权"):
        SingingPipeline(FakeSingingEngine(), projects_root=tmp_path).train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "source_asset_ids": ["asset"], "engine": "rvc_v2"})


def test_cancelled_training_cleans_staging_and_allows_retry(tmp_path):
    project, _ = _fixture(tmp_path)
    pipeline = SingingPipeline(CancellingEngine(), projects_root=tmp_path)
    cancel = threading.Event()
    with pytest.raises(RuntimeError, match="取消"):
        pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "cancelled", "source_asset_ids": ["asset"], "engine": "rvc_v2"}, cancel=cancel)
    root = project / "models" / "singing" / "profile"
    assert not (root / "cancelled.staging").exists()
    assert not (root / "cancelled").exists()
    assert pipeline.engine is not None


def test_verification_failure_is_persisted_but_never_active(tmp_path):
    project, _ = _fixture(tmp_path)
    pipeline = SingingPipeline(FailingVerificationEngine(), projects_root=tmp_path)
    with pytest.raises(RuntimeError, match="验证失败"):
        pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "failed", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    value = json.loads((project / "project.json").read_text(encoding="utf-8")); profile = value["voice_profiles"][0]
    assert profile.get("active_singing_model_id", "") == ""
    assert profile["singing_models"][0]["trust_status"] == "verification_failed"


def test_verification_exception_removes_unregistered_final_directory(tmp_path):
    project, _ = _fixture(tmp_path)
    pipeline = SingingPipeline(ExplodingVerificationEngine(), projects_root=tmp_path)
    with pytest.raises(RuntimeError, match="verification crashed"):
        pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "crashed", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    root = project / "models" / "singing" / "profile"
    assert not (root / "crashed").exists() and not (root / "crashed.staging").exists()


def test_ai_vocal_cache_pitch_and_lineage(tmp_path):
    project, cover = _fixture(tmp_path); engine = FakeSingingEngine(); pipeline = SingingPipeline(engine, projects_root=tmp_path)
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    first = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"], "pitch_shift": 0})
    second = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"], "pitch_shift": 0})
    shifted = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"], "pitch_shift": 2})
    assert first["cache_hit"] is False and second["cache_hit"] is True and shifted["cache_hit"] is False
    assert first["asset_id"] != shifted["asset_id"]
    restored = CoverProject.load(project, cover.id); ai = next(item for item in restored.assets if item.id == first["asset_id"])
    assert ai.content_origin == "ai_generated" and ai.source_asset_ids == ["vocal"] and ai.model_id == model["id"]
    ai.content_origin = "original"; restored.save()
    regenerated = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"], "pitch_shift": 0, "output_id": "after-tamper"})
    assert regenerated["cache_hit"] is False and regenerated["asset_id"] == "after-tamper"


def test_ai_vocal_cache_includes_rvc_product_settings(tmp_path):
    project, cover = _fixture(tmp_path); engine = FakeSingingEngine(); pipeline = SingingPipeline(engine, projects_root=tmp_path)
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "settings", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    base = {"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"]}
    first = pipeline.convert(base)
    changed = pipeline.convert({**base, "index_rate": 0.6})
    assert first["cache_hit"] is False and changed["cache_hit"] is False


def test_ai_vocal_cache_includes_cleanup_engine_lineage(tmp_path):
    project, cover = _fixture(tmp_path); pipeline = SingingPipeline(FakeSingingEngine(), projects_root=tmp_path)
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "cleanup", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    vocal = cover.root / "stems" / "vocals.wav"; cleaned = cover.root / "stems" / "cleanup.wav"; cleaned.write_bytes(vocal.read_bytes())
    digest = hashlib.sha256(cleaned.read_bytes()).hexdigest()
    cover.add_asset(CoverAsset("cleaned", "vocal", "stems/cleanup.wav", digest, "separated", "ffmpeg-afftdn", "cleanup-one", source_asset_ids=["vocal"]))
    base = {"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"]}
    first = pipeline.convert(base)
    restored = CoverProject.load(project, cover.id); assert restored.get_asset(first["asset_id"]).source_asset_ids == ["cleaned"]
    restored.get_asset("cleaned").producer_version = "cleanup-two"; restored.save()
    assert CoverProject.load(project, cover.id).get_asset("cleaned").producer_version == "cleanup-two"
    changed = pipeline.convert({**base, "output_id": "cleanup-lineage-two"})
    assert first["cache_hit"] is False and changed["cache_hit"] is False


def test_suggest_transpose_never_changes_conversion_settings(tmp_path):
    project, cover = _fixture(tmp_path); pipeline = SingingPipeline(PitchEngine(), projects_root=tmp_path)
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "pitch", "source_asset_ids": ["asset"], "engine": "rvc_v2"})
    result = pipeline.suggest_transpose({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id, "singing_model_id": model["id"]})
    assert result["suggested_transpose"] == 12
    assert result["applied"] is False
