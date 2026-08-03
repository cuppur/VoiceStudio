from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from local_voice_studio.audio import copy_original, scan_audio_files, sha256_file
from local_voice_studio.models import DatasetManifest, DatasetSegment, Job, JobKind, JobStatus, ReferenceAsset, SourceAsset, VoiceProfile
from local_voice_studio.paths import AppPaths, ensure_within
from local_voice_studio.protocol import Message
from local_voice_studio.runtime import EngineRuntimeError, EngineRuntimeResolver
from local_voice_studio.storage import StudioStore
from local_voice_studio.text import split_text


def write_wav(path: Path, seconds: float = 1.0, sample_rate: int = 16000) -> None:
    frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(sample_rate); stream.writeframes(b"\x00\x00" * frames)


class TextTests(unittest.TestCase):
    def test_chinese_and_english_split(self):
        parts = split_text("你好，世界！This is a test. 下一句很长吗？", max_chars=18)
        self.assertEqual("".join(parts), "你好，世界！This is a test. 下一句很长吗？")
        self.assertTrue(all(len(item) <= 18 for item in parts))

    def test_empty(self):
        self.assertEqual(split_text(" \n "), [])

    def test_hard_split_keeps_content(self):
        text = "这是一段没有句号但是包含，多个逗号而且长度非常长的文字用来测试安全切分"
        parts = split_text(text, 12)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(item) <= 12 for item in parts))


class AudioTests(unittest.TestCase):
    def test_probe_and_dedupe_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); first = root / "a.wav"; second = root / "b.wav"; write_wav(first, 1.25); second.write_bytes(first.read_bytes())
            probes = scan_audio_files([root])
            self.assertEqual(len(probes), 2); self.assertAlmostEqual(probes[0].duration_seconds, 1.25, places=2)
            self.assertTrue(any(item.duplicate_of for item in probes))
            raw = root / "raw"; copied1 = copy_original(first, raw); copied2 = copy_original(second, raw)
            self.assertNotEqual(copied1, copied2); self.assertEqual(first.read_bytes(), copied1.read_bytes()); self.assertEqual(sha256_file(first), sha256_file(copied1))


class ModelTests(unittest.TestCase):
    def test_training_gate(self):
        short = DatasetManifest("short", "voice", [DatasetSegment("x", "a.wav", 0, 59, text="测试", approved=True, human_confirmed=True)], frozen=True)
        self.assertFalse(short.can_train()[0])
        enough = DatasetManifest("ok", "voice", [DatasetSegment("x", "a.wav", 0, 60, text="测试", approved=True, human_confirmed=True)], frozen=True)
        self.assertTrue(enough.can_train()[0])
        unconfirmed = DatasetManifest("no", "voice", [DatasetSegment("x", "a.wav", 0, 80, text="测试", approved=True)], frozen=True)
        self.assertFalse(unconfirmed.can_train()[0])

    def test_protocol_roundtrip(self):
        message = Message("health", {"中文": True}, "abc")
        parsed = Message.decode(message.encode())
        self.assertEqual(parsed.id, "abc"); self.assertEqual(parsed.payload["中文"], True)
        bom = Message.decode('\ufeff{"id":"中文","type":"health","payload":{"路径":"声音库"}}')
        self.assertEqual(bom.id, "中文"); self.assertEqual(bom.payload["路径"], "声音库")


class StorageTests(unittest.TestCase):
    def test_project_profile_and_job_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3")
            store = StudioStore(paths); project = store.create_project('测<试>:项目')
            self.assertTrue(project.name.startswith("测试项目"))
            profile = VoiceProfile("声音", True, reference_assets=[ReferenceAsset("a.wav", "hash", "你好", approved=True)])
            store.save_profile(project, profile); loaded = store.list_profiles(project)
            self.assertEqual(loaded[0].name, "声音"); self.assertEqual(loaded[0].reference_assets[0].transcript, "你好")
            job = Job(JobKind.SYNTHESIZE, {"text": "你好"}); job.status = JobStatus.COMPLETED; job.outputs = ["out.wav"]; store.save_job(job)
            restored = store.list_jobs()[0]; self.assertEqual(restored.status, JobStatus.COMPLETED); self.assertEqual(restored.outputs, ["out.wav"])

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "safe"; root.mkdir()
            with self.assertRaises(ValueError): ensure_within(root, root / ".." / "outside")

    def test_source_asset_and_snapshot_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3"); store = StudioStore(paths); project = store.create_project("声音")
            profile = VoiceProfile("声音", True); source = root / "导入.wav"; write_wav(source, 61); asset = SourceAsset(profile.id, str(source), str(source), sha256_file(source), duration_seconds=61, sample_rate=16000, channels=1, codec="pcm"); profile.source_asset_ids = [asset.id]; store.save_source_assets(project, [asset]); store.save_profile(project, profile)
            self.assertEqual(store.list_source_assets(project, profile.id)[0].id, asset.id)
            dataset = DatasetManifest("快照", profile.id, [DatasetSegment(asset.sha256, str(source), 0, 61, text="确认文本", asr_text="确认文本", approved=True, included=True, human_confirmed=True)], frozen=True, snapshot_sha256="abc")
            store.save_dataset_snapshot(project, dataset); self.assertTrue(store.load_dataset_snapshot(project, dataset.id).can_train()[0])

    def test_legacy_reference_migration(self):
        legacy = {"schema_version": 1, "voice_profiles": [{"id": "p", "name": "旧声音", "consent_confirmed": True, "reference_assets": [{"path": "旧.wav", "sha256": "h", "transcript": "你好", "approved": True}]}]}
        migrated = StudioStore._migrate_project(legacy)
        self.assertEqual(migrated["schema_version"], 2); self.assertEqual(len(migrated["source_assets"]), 1); self.assertTrue(migrated["voice_profiles"][0]["source_asset_ids"])


class RuntimeResolverTests(unittest.TestCase):
    def test_installed_private_python_imports_torch(self):
        python = AppPaths.default().private_python
        if not python.is_file(): self.skipTest("私有运行时尚未安装")
        completed = subprocess.run([str(python), "-X", "utf8", "-c", "import torch; assert torch.__version__ == '2.7.1+cu128'"], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_frozen_uses_private_python_not_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root, root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "db"); private = paths.runtime_root / "env" / "python.exe"; private.parent.mkdir(parents=True); private.write_bytes(b"python")
            launch = EngineRuntimeResolver(paths, frozen=True, executable=str(root / "LocalVoiceStudio.exe"), bundle_root=root / "bundle").worker_launch()
            self.assertEqual(launch.program, private.resolve()); self.assertNotEqual(launch.program.name, "LocalVoiceStudio.exe"); self.assertEqual(launch.arguments[:2], ["-X", "utf8"])

    def test_frozen_without_runtime_fails_actionably(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root, root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "db")
            with self.assertRaisesRegex(EngineRuntimeError, "安装/修复"): EngineRuntimeResolver(paths, frozen=True, executable=str(root / "LocalVoiceStudio.exe")).worker_launch()


class WorkerIntegrationTests(unittest.TestCase):
    def test_worker_health_protocol(self):
        env = os.environ.copy(); source = str(Path(__file__).resolve().parents[1] / "src"); env["PYTHONPATH"] = source; env["LOCAL_VOICE_STUDIO_HOME"] = tempfile.mkdtemp(); env["LOCAL_VOICE_STUDIO_PROJECTS"] = tempfile.mkdtemp()
        process = subprocess.Popen([sys.executable, "-u", "-m", "local_voice_studio.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        try:
            ready = json.loads(process.stdout.readline()); self.assertEqual(ready["type"], "ready")
            process.stdin.write('{"id":"health-test","type":"health","payload":{}}\n'); process.stdin.flush()
            response = json.loads(process.stdout.readline()); self.assertEqual(response["id"], "health-test"); self.assertEqual(response["type"], "result"); self.assertIn("engine", response["payload"])
            required = {"python_executable", "python_version", "torch_version", "cuda_available", "cuda_version", "gpu_name", "compute_capability", "tensor_test_passed", "gpt_sovits_imported", "models_ready", "ffmpeg_ready", "compatible", "actionable_errors"}
            self.assertTrue(required.issubset(response["payload"]))
            process.stdin.write('{"id":"stop","type":"shutdown","payload":{}}\n'); process.stdin.flush(); process.stdout.readline()
        finally:
            process.terminate(); process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
