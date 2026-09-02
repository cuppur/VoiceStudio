"""Phase 3.1 product-contract smoke through Store, WorkerService and CoverProject."""
from __future__ import annotations

import hashlib
import json
import tempfile
import wave
from pathlib import Path

from local_voice_studio.cover.project import CoverProject
from local_voice_studio.models import SourceAsset, VoiceProfile
from local_voice_studio.paths import AppPaths
from local_voice_studio.protocol import Message
from local_voice_studio.storage import StudioStore
from local_voice_studio.worker import WorkerService


def write_wav(path: Path, seconds: float, sample: bytes = b"\x01\x00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(100)
        stream.writeframes(sample * int(seconds * 100))


class SmokeEngine:
    def readiness(self):
        from local_voice_studio.singing.base import EngineReadiness
        return EngineReadiness(True)

    def train(self, payload, cancel=None):
        root = Path(payload["experiment_dir"]); model = root / "model.pth"; index = root / "model.index"
        model.write_bytes(b"controlled-model"); index.write_bytes(b"controlled-index")
        return [model, index]

    def verify_model(self, checkpoint, index):
        return checkpoint.is_file() and index.is_file()

    def convert(self, payload, cancel=None):
        source, output = Path(payload["input_path"]), Path(payload["output_path"])
        with wave.open(str(source), "rb") as reader:
            rate, channels, width, frames = reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()
        with wave.open(str(output), "wb") as writer:
            writer.setnchannels(channels); writer.setsampwidth(width); writer.setframerate(rate)
            writer.writeframes(b"\x02\x00" * frames * channels)
        return output

    def cancel(self):
        return None


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="voicestudio-phase31-") as temporary:
        root = Path(temporary); data = root / "data"
        paths = AppPaths(data, root / "projects", data / "runtime", data / "engine", data / "models", data / "logs", data / "studio.sqlite3")
        store = StudioStore(paths); project = store.create_project("Phase 3.1 smoke")
        profile = VoiceProfile("授权声音", True, id="profile", consent_record="本人或明确授权", consent_confirmed_at="2026-09-01T00:00:00Z")
        store.save_profile(project, profile)
        source = project / "raw" / profile.id / "source.wav"; write_wav(source, 180)
        asset = SourceAsset(profile.id, str(source), str(source), hashlib.sha256(source.read_bytes()).hexdigest(), id="asset", duration_seconds=180, sample_rate=100, channels=1, codec="pcm")
        store.save_source_assets(project, [asset]); profile.source_asset_ids = [asset.id]; store.save_profile(project, profile)
        song = root / "song.wav"; write_wav(song, 5); cover = CoverProject.create(project, title="song"); cover.copy_source(song)
        vocal = cover.root / "stems" / "vocals.wav"; write_wav(vocal, 5); cover.set_stem("vocal", vocal); cover.attest_rights()
        service = WorkerService(paths, singing_engine=SmokeEngine()); events = []
        service.emit = lambda request_id, event, payload: events.append({"id": request_id, "type": event, "payload": payload})
        train_payload = {"project_path": str(project), "profile_id": profile.id, "source_asset_ids": [asset.id], "training_run_id": "run", "engine": "rvc_v2"}
        service.handle(Message("train_singing_model", train_payload, id="train")); service.current_thread.join(10)
        trained = next(item for item in events if item["id"] == "train" and item["type"] == "result")
        model_id = trained["payload"]["model"]["id"]
        service.handle(Message("convert_vocal", {"project_path": str(project), "profile_id": profile.id, "cover_id": cover.id, "singing_model_id": model_id, "pitch_shift": 0}, id="convert")); service.current_thread.join(10)
        converted = next(item for item in events if item["id"] == "convert" and item["type"] == "result")
        restored = CoverProject.load(project, cover.id); ai = restored.get_asset(role="ai_vocal")
        report = {"training_request_keys": sorted(train_payload), "model_trust": trained["payload"]["model"]["trust_status"], "dataset_sha256": trained["payload"]["model"]["training_dataset_sha256"], "ai_asset": ai.to_dict() if ai else None, "conversion": converted["payload"]}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ai and report["model_trust"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
