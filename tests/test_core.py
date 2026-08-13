from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import shutil
from pathlib import Path

from local_voice_studio.audio import copy_original, scan_audio_files, sha256_file
from local_voice_studio import __version__
from local_voice_studio.models import DatasetManifest, DatasetSegment, Job, JobKind, JobStatus, ReferenceAsset, SourceAsset, VoiceProfile, dataset_snapshot_sha256
from local_voice_studio.paths import AppPaths, ensure_within
from local_voice_studio.protocol import Message
from local_voice_studio.runtime import EngineRuntimeError, EngineRuntimeResolver
from local_voice_studio.storage import StudioStore
from local_voice_studio.text import split_text
from local_voice_studio.training import TrainingPipeline
from local_voice_studio.worker import WorkerService


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

    def test_package_version_matches_project_version(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual(__version__, "0.3.0")
        self.assertIn(f'version = "{__version__}"', pyproject)


class StorageTests(unittest.TestCase):
    def test_project_profile_and_job_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3")
            store = StudioStore(paths); project = store.create_project('测<试>:项目')
            self.assertTrue(project.name.startswith("测试项目"))
            profile = VoiceProfile("声音", True, reference_assets=[ReferenceAsset("a.wav", "hash", "你好", approved=True)])
            profile.candidate_training_run_id = "run-1"; profile.candidate_snapshot_sha256 = "snapshot-hash"; profile.ab_status = "awaiting_ab"; profile.ab_base_outputs = ["base.wav"]
            store.save_profile(project, profile); loaded = store.list_profiles(project)
            self.assertEqual(loaded[0].name, "声音"); self.assertEqual(loaded[0].reference_assets[0].transcript, "你好")
            self.assertEqual(loaded[0].candidate_training_run_id, "run-1"); self.assertEqual(loaded[0].ab_status, "awaiting_ab"); self.assertEqual(loaded[0].ab_base_outputs, ["base.wav"])
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
            dataset = DatasetManifest("快照", profile.id, frozen=True)
            snapshot_root = project / "datasets" / dataset.id; audio = snapshot_root / "audio" / "frozen.wav"; audio.parent.mkdir(parents=True); shutil.copy2(source, audio)
            relative = audio.relative_to(project).as_posix(); dataset.segments = [DatasetSegment(sha256_file(audio), str(audio), 0, 61, text="确认文本", asr_text="确认文本", approved=True, included=True, human_confirmed=True, audio_relative_path=relative)]
            list_path = snapshot_root / "dataset.list"; list_path.write_text(f"{relative}|speaker|zh|确认文本\n", encoding="utf-8")
            dataset.list_path = str(list_path); dataset.wav_dir = str(audio.parent); dataset.list_relative_path = list_path.relative_to(project).as_posix(); dataset.list_sha256 = sha256_file(list_path); dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
            manifest_path = store.save_dataset_snapshot(project, dataset)
            value = json.loads(manifest_path.read_text(encoding="utf-8")); value.update({"approved_seconds": 61, "future_extension": True}); manifest_path.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(store.load_dataset_snapshot(project, dataset.id).can_train()[0])
            summary_path = store.load_project(project)["dataset_snapshots"][0]["path"]; self.assertFalse(Path(summary_path).is_absolute())

    def test_remove_source_asset_deletes_only_project_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3"); store = StudioStore(paths); project = store.create_project("remove")
            original = root / "original.wav"; write_wav(original)
            copied = project / "raw" / "voice" / "copy.wav"; copied.parent.mkdir(parents=True); shutil.copy2(original, copied)
            profile = VoiceProfile("voice", True, id="voice"); asset = SourceAsset(profile.id, str(original), str(copied), sha256_file(copied)); profile.source_asset_ids = [asset.id]
            store.save_profile(project, profile); store.save_source_assets(project, [asset])
            removed = store.remove_source_assets(project, {asset.id})
            self.assertEqual([item.id for item in removed], [asset.id]); self.assertTrue(original.is_file()); self.assertFalse(copied.exists())
            self.assertFalse(store.list_source_assets(project)); self.assertFalse(store.list_profiles(project)[0].source_asset_ids)

    def test_snapshot_survives_project_move_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects-a", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3"); store = StudioStore(paths); project = store.create_project("可迁移")
            dataset = DatasetManifest("快照", "voice", frozen=True); snapshot_root = project / "datasets" / dataset.id; audio = snapshot_root / "audio" / "clip.wav"; audio.parent.mkdir(parents=True); write_wav(audio, 61)
            relative = audio.relative_to(project).as_posix(); dataset.segments = [DatasetSegment(sha256_file(audio), str(audio), 0, 61, text="人工确认", asr_text="人工确认", approved=True, human_confirmed=True, audio_relative_path=relative)]
            list_path = snapshot_root / "dataset.list"; list_path.write_text(f"{relative}|speaker|zh|人工确认\n", encoding="utf-8"); dataset.list_path = str(list_path); dataset.wav_dir = str(audio.parent); dataset.list_relative_path = list_path.relative_to(project).as_posix(); dataset.list_sha256 = sha256_file(list_path); dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset); store.save_dataset_snapshot(project, dataset)
            moved_root = root / "projects-b"; moved_project = moved_root / project.name; moved_root.mkdir(); shutil.copytree(project, moved_project)
            moved_paths = AppPaths(root / "data-b", moved_root, root / "runtime", root / "engine", root / "models", root / "logs-b", root / "data-b/db.sqlite3"); moved_store = StudioStore(moved_paths)
            loaded = moved_store.load_dataset_snapshot(moved_project, dataset.id); self.assertEqual(Path(loaded.segments[0].audio_path).parent.name, "audio")
            moved_list = moved_project / loaded.list_relative_path; moved_list.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "标注清单"):
                moved_store.load_dataset_snapshot(moved_project, dataset.id)

    def test_legacy_absolute_snapshot_is_migrated_into_snapshot_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); paths = AppPaths(root / "data", root / "projects", root / "runtime", root / "engine", root / "models", root / "logs", root / "data/db.sqlite3"); store = StudioStore(paths); project = store.create_project("旧项目")
            source = root / "legacy.wav"; write_wav(source, 61); dataset = DatasetManifest("旧快照", "voice", frozen=True, schema_version=1); snapshot_root = project / "datasets" / dataset.id; snapshot_root.mkdir(parents=True)
            segment = DatasetSegment(sha256_file(source), str(source), 0, 61, text="旧文本", asr_text="旧文本", approved=True, human_confirmed=True); dataset.segments = [segment]
            (snapshot_root / "manifest.json").write_text(json.dumps(dataset.to_dict(), ensure_ascii=False), encoding="utf-8")
            loaded = store.load_dataset_snapshot(project, dataset.id)
            self.assertEqual(loaded.schema_version, 2); self.assertTrue(Path(loaded.segments[0].audio_path).is_relative_to(snapshot_root / "audio")); self.assertTrue(loaded.snapshot_sha256)

    def test_legacy_reference_migration(self):
        legacy = {"schema_version": 1, "voice_profiles": [{"id": "p", "name": "旧声音", "consent_confirmed": True, "reference_assets": [{"path": "旧.wav", "sha256": "h", "transcript": "你好", "approved": True}]}]}
        migrated = StudioStore._migrate_project(legacy)
        self.assertEqual(migrated["schema_version"], 3); self.assertEqual(len(migrated["source_assets"]), 1); self.assertTrue(migrated["voice_profiles"][0]["source_asset_ids"]); self.assertIn("workflows", migrated)


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
    def test_preparation_runs_have_disjoint_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); first = TrainingPipeline.preparation_paths(root, "voice", "run-a"); second = TrainingPipeline.preparation_paths(root, "voice", "run-b")
            self.assertNotEqual(first["normalized"], second["normalized"]); self.assertNotEqual(first["segments"], second["segments"]); self.assertNotEqual(first["manifest"], second["manifest"])
            first["normalized"].mkdir(parents=True); (first["normalized"] / "old.wav").write_bytes(b"old")
            second["normalized"].mkdir(parents=True); self.assertFalse((second["normalized"] / "old.wav").exists())

    def test_feature_manifest_is_bound_to_exact_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); phoneme = root / "2-name2text.txt"; semantic = root / "6-name2semantic.tsv"; phoneme.write_text("phoneme", encoding="utf-8"); semantic.write_text("semantic", encoding="utf-8")
            manifest = root / "feature-manifest.json"; value = {"profile_id": "voice", "dataset_snapshot_id": "snapshot-a", "snapshot_sha256": "sha-a", "list_sha256": "list-a", "feature_files": {"phoneme": str(phoneme), "semantic": str(semantic)}}; manifest.write_text(json.dumps(value), encoding="utf-8")
            payload = {"profile_id": "voice", "dataset_snapshot_id": "snapshot-a", "snapshot_sha256": "sha-a", "list_sha256": "list-a"}; self.assertEqual(TrainingPipeline._validate_feature_manifest(manifest, payload)["snapshot_sha256"], "sha-a")
            for key, bad in (("profile_id", "other"), ("dataset_snapshot_id", "snapshot-b"), ("snapshot_sha256", "sha-b"), ("list_sha256", "list-b")):
                changed = dict(payload); changed[key] = bad
                with self.assertRaisesRegex(RuntimeError, "其他数据集"):
                    TrainingPipeline._validate_feature_manifest(manifest, changed)
            semantic.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "缺失或为空"):
                TrainingPipeline._validate_feature_manifest(manifest, payload)

    def test_fresh_training_views_do_not_reuse_run_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); features = root / "features"; features.mkdir(); (features / "2-name2text.txt").write_text("features", encoding="utf-8"); (features / "feature-manifest.json").write_text("{}", encoding="utf-8")
            first = root / "runs" / "one" / "sovits-exp"; second = root / "runs" / "two" / "sovits-exp"; TrainingPipeline._materialize_feature_view(features, first); (first / "logs_s2_v2ProPlus").mkdir(); (first / "logs_s2_v2ProPlus" / "old.ckpt").write_bytes(b"old")
            TrainingPipeline._materialize_feature_view(features, second)
            self.assertTrue((second / "2-name2text.txt").is_file()); self.assertFalse((second / "logs_s2_v2ProPlus").exists()); self.assertFalse((second / "feature-manifest.json").exists())

    def test_feature_shards_are_merged_to_training_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2-name2text-0.txt").write_text("one\n", encoding="utf-8")
            (root / "2-name2text-1.txt").write_text("two\n", encoding="utf-8")
            TrainingPipeline._merge_feature_shards(root, "2-name2text-*.txt", root / "2-name2text.txt", False)
            self.assertEqual((root / "2-name2text.txt").read_text(encoding="utf-8"), "one\ntwo\n")
            (root / "6-name2semantic-0.tsv").write_text("name\tsemantic\na\t1\n", encoding="utf-8")
            (root / "6-name2semantic-1.tsv").write_text("name\tsemantic\nb\t2\n", encoding="utf-8")
            TrainingPipeline._merge_feature_shards(root, "6-name2semantic-*.tsv", root / "6-name2semantic.tsv", True)
            self.assertEqual((root / "6-name2semantic.tsv").read_text(encoding="utf-8"), "name\tsemantic\na\t1\nb\t2\n")
            self.assertEqual(TrainingPipeline.training_run_root(root, "run-id"), root.resolve() / "train-runs" / "run-id")

    def test_frozen_snapshot_rejects_modified_audio_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); dataset = DatasetManifest("快照", "voice", frozen=True); snapshot_root = root / "datasets" / dataset.id; audio = snapshot_root / "audio" / "frozen.wav"; audio.parent.mkdir(parents=True); write_wav(audio, 61); original_audio = audio.read_bytes(); list_path = snapshot_root / "dataset.list"
            relative = audio.relative_to(root).as_posix(); segment = DatasetSegment(sha256_file(audio), str(audio), 0, 61, text="人工确认文本", asr_text="人工确认文本", approved=True, included=True, human_confirmed=True, audio_relative_path=relative)
            list_path.write_text(f"{relative}|speaker|zh|人工确认文本\n", encoding="utf-8")
            dataset.segments = [segment]; dataset.list_path = str(list_path); dataset.wav_dir = str(audio.parent); dataset.list_relative_path = list_path.relative_to(root).as_posix(); dataset.list_sha256 = sha256_file(list_path)
            dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
            payload = {**dataset.to_dict(), "dataset_snapshot_id": dataset.id, "project_path": str(root)}
            WorkerService._validate_dataset_snapshot(payload)
            audio.write_bytes(audio.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "音频已被修改"):
                WorkerService._validate_dataset_snapshot(payload)
            audio.write_bytes(original_audio); list_path.write_text(f"{relative}|speaker|zh|被篡改文本\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "标注清单已被修改"):
                WorkerService._validate_dataset_snapshot(payload)

    def test_checkpoint_selection_is_scoped_to_current_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old = root / "old.ckpt"; old.write_bytes(b"old")
            run = root / "run-new"; run.mkdir(); first = run / "epoch-1.ckpt"; latest = run / "epoch-2.ckpt"; first.write_bytes(b"first"); latest.write_bytes(b"latest")
            os.utime(first, ns=(1_000_000_000, 1_000_000_000)); os.utime(latest, ns=(2_000_000_000, 2_000_000_000)); os.utime(old, ns=(3_000_000_000, 3_000_000_000))
            self.assertEqual(TrainingPipeline._latest_checkpoint(run, ".ckpt"), latest)

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
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None: process.terminate(); process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
