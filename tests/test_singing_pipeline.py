import hashlib
import json
from pathlib import Path

import pytest

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.singing.pipeline import SingingPipeline


class FakeSingingEngine:
    def train(self, payload, cancel=None):
        path = Path(payload["experiment_dir"]) / "model.pth"
        path.write_bytes(b"fake checkpoint")
        return [path]

    def convert(self, payload, cancel=None):
        path = Path(payload["output_path"])
        path.write_bytes(b"RIFFfake audio")
        return path


def _fixture(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cover = CoverProject.create(project, title="song")
    source = tmp_path / "original.wav"
    source.write_bytes(b"RIFForiginal")
    cover.copy_source(source)
    vocal = cover.root / "stems" / "vocals.wav"
    vocal.write_bytes(b"RIFFvocal")
    cover.set_stem("vocal", vocal)
    cover.attest_rights()
    (project / "project.json").write_text(json.dumps({"voice_profiles": [{
        "id": "profile", "name": "Authorized", "consent_confirmed": True,
        "consent_confirmed_at": "now", "consent_record": "test", "singing_models": []
    }]}), encoding="utf-8")
    return project, cover


def test_train_and_convert_registers_ai_asset(tmp_path):
    project, cover = _fixture(tmp_path)
    pipeline = SingingPipeline(FakeSingingEngine())
    model = pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "training_dataset_sha256": hashlib.sha256(b"dataset").hexdigest()})
    result = pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id})
    restored = CoverProject.load(project, cover.id)
    asset = restored.get_asset(role="ai_vocal")
    assert model["profile_id"] == "profile"
    assert result["content_origin"] == "ai_generated"
    assert asset and asset.content_origin == "ai_generated" and Path(result["output_path"]).is_file()


def test_conversion_requires_song_rights(tmp_path):
    project, cover = _fixture(tmp_path)
    pipeline = SingingPipeline(FakeSingingEngine())
    pipeline.train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "training_dataset_sha256": hashlib.sha256(b"dataset").hexdigest()})
    cover.attest_rights(False)
    with pytest.raises(ValueError, match="权利"):
        pipeline.convert({"project_path": str(project), "profile_id": "profile", "cover_id": cover.id})


def test_training_rejects_unconsented_profile(tmp_path):
    project, _ = _fixture(tmp_path)
    manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
    manifest["voice_profiles"][0]["consent_confirmed"] = False
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="授权"):
        SingingPipeline(FakeSingingEngine()).train({"project_path": str(project), "profile_id": "profile", "training_run_id": "run", "training_dataset_sha256": hashlib.sha256(b"dataset").hexdigest()})
