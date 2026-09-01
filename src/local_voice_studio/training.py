from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import hashlib
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .models import utc_now
from .paths import AppPaths, ensure_within, validate_id, validate_sha256

# Singing/RVC is deliberately imported lazily: the public desktop process must
# remain free of torch and other GPU dependencies.
def rvc_training_engine(config, runner=None):
    from .singing.rvc import RVCEngine
    return RVCEngine(config, runner=runner)


class TrainingPipeline:
    """Direct subprocess orchestration for the pinned GPT-SoVITS training scripts."""

    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.process: subprocess.Popen | None = None

    def cancel(self) -> None:
        process = self.process
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                process.terminate()

    @staticmethod
    def preparation_paths(project: Path, profile_id: str, preparation_id: str) -> dict[str, Path]:
        """Return paths owned by one immutable preparation run."""
        project = project.resolve()
        validate_id(profile_id, legacy=True, field="profile_id")
        validate_id(preparation_id, legacy=True, field="preparation_id")
        run_root = ensure_within(project, project / "processed" / profile_id / "runs" / preparation_id)
        working_root = ensure_within(project, project / "datasets" / "working" / profile_id / preparation_id)
        return {
            "run_root": run_root,
            "normalized": run_root / "normalized",
            "segments": run_root / "segments",
            "working_root": working_root,
            "asr": working_root / "asr",
            "manifest": working_root / "preparation.json",
        }

    @staticmethod
    def training_run_root(data_root: Path, training_run_id: str) -> Path:
        # Keep this deliberately short. Upstream feature names include the
        # complete sliced-audio filename and otherwise exceed MAX_PATH on
        # standard Windows installations.
        validate_id(training_run_id, legacy=True, field="training_run_id")
        return ensure_within(data_root.resolve(), data_root.resolve() / "train-runs" / training_run_id)

    def _run(self, command: list[str], env: dict[str, str], log: Callable[[str], None], cancel: threading.Event) -> None:
        process_env = {**os.environ, **env}
        # Upstream GPT-SoVITS helper scripts invoke ffmpeg by name and load DLLs
        # from the private Conda environment.  A packaged application must not
        # depend on the user's PATH, so make those locations explicit here.
        private_env = self.paths.runtime_root / "env"
        path_entries = [
            self.paths.data_root / "tools",
            private_env,
            private_env / "Scripts",
            private_env / "Library" / "bin",
        ]
        process_env["PATH"] = os.pathsep.join(
            [str(item) for item in path_entries if item.exists()]
        )
        process_env["PYTHONUTF8"] = "1"
        process_env["PYTHONIOENCODING"] = "utf-8"
        process_env.setdefault("HF_HOME", str(self.paths.models_root / "huggingface"))
        process_env.setdefault("MODELSCOPE_CACHE", str(self.paths.models_root / "modelscope"))
        process_env.setdefault("TORCH_HOME", str(self.paths.models_root / "torch"))
        existing_pythonpath = process_env.get("PYTHONPATH", "")
        process_env["PYTHONPATH"] = os.pathsep.join(
            [
                str(self.paths.engine_root),
                str(self.paths.engine_root / "GPT_SoVITS"),
                *([existing_pythonpath] if existing_pythonpath else []),
            ]
        )
        self.process = subprocess.Popen(
            command, cwd=self.paths.engine_root, env=process_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
        assert self.process.stdout is not None
        for line in self.process.stdout:
            log(line.rstrip())
            if cancel.is_set():
                self.cancel()
                raise RuntimeError("任务已取消")
        code = self.process.wait()
        self.process = None
        if code:
            raise RuntimeError(f"训练子进程退出码 {code}")

    def prepare(self, payload: dict, progress: Callable[[float, str], None], cancel: threading.Event) -> Path:
        engine = self.paths.engine_root
        python = self.paths.private_python
        if not python.exists():
            raise RuntimeError("私有 Python 运行时尚未安装")
        action = payload.get("action", "format")
        if action == "pipeline":
            return self._prepare_source_assets(payload, progress, cancel)
        if action in {"asr", "denoise", "uvr", "slice"}:
            input_dir = Path(payload["input_dir"]).resolve()
            output_dir = Path(payload["output_dir"]).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            if action == "asr":
                progress(0.05, "开始本地自动转写")
                command = [str(python), "-X", "utf8", "-u", "-m", "local_voice_studio.asr_cli", "-i", str(input_dir), "-o", str(output_dir), "-l", str(payload.get("language", "zh"))]
            elif action == "denoise":
                progress(0.05, "开始降噪；原文件不会被覆盖")
                command = [str(python), "-s", "tools/cmd-denoise.py", "-i", str(input_dir), "-o", str(output_dir), "-p", "float16"]
            elif action == "uvr":
                progress(0.05, "开始伴奏/人声分离；原文件不会被覆盖")
                command = [str(python), "-m", "local_voice_studio.uvr_cli", "--engine", str(engine), "--input", str(input_dir), "--output", str(output_dir)]
            else:
                progress(0.05, "开始按静音自动切分")
                command = [str(python), "-s", "tools/slice_audio.py", str(input_dir), str(output_dir), "-34", "4000", "300", "10", "500", "0.9", "0.25", "0", "1"]
            self._run(command, {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.5, line), cancel)
            progress(1.0, "处理完成")
            if action == "asr":
                lists = sorted(output_dir.glob("*.list"), key=lambda item: item.stat().st_mtime, reverse=True)
                return lists[0] if lists else output_dir
            return output_dir
        exp_name = payload["experiment_name"]
        profile_id = validate_id(str(payload["profile_id"]), legacy=True, field="profile_id"); snapshot_id = validate_id(str(payload["dataset_snapshot_id"]), legacy=True, field="dataset_snapshot_id"); snapshot_sha256 = validate_sha256(str(payload["snapshot_sha256"]), field="snapshot_sha256")
        exp_dir = self.paths.data_root / "training" / profile_id / snapshot_sha256 / "features"
        exp_dir.mkdir(parents=True, exist_ok=True)
        list_path = exp_dir / "runtime.list"
        list_path.write_text("\n".join(f"{item['audio_path']}|speaker|{item.get('language', 'zh')}|{item.get('text', '')}" for item in payload["segments"]) + "\n", encoding="utf-8")
        wav_dir = Path(payload["wav_dir"]).resolve()
        common = {
            "inp_text": str(list_path), "inp_wav_dir": str(wav_dir), "exp_name": exp_name,
            "opt_dir": str(exp_dir), "i_part": "0", "all_parts": "1",
            "_CUDA_VISIBLE_DEVICES": "0", "is_half": "True",
        }
        stages = [
            (0.08, "文本与音素处理", "GPT_SoVITS/prepare_datasets/1-get-text.py", {
                "bert_pretrained_dir": str(engine / "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large")}),
            (0.35, "自监督特征提取", "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py", {
                "cnhubert_base_dir": str(engine / "GPT_SoVITS/pretrained_models/chinese-hubert-base"),
                "sv_path": str(engine / "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt")}),
            (0.56, "说话人特征提取", "GPT_SoVITS/prepare_datasets/2-get-sv.py", {
                "cnhubert_base_dir": str(engine / "GPT_SoVITS/pretrained_models/chinese-hubert-base"),
                "sv_path": str(engine / "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt")}),
            (0.72, "语义 Token 提取", "GPT_SoVITS/prepare_datasets/3-get-semantic.py", {
                "pretrained_s2G": str(engine / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
                "s2config_path": "GPT_SoVITS/configs/s2v2ProPlus.json"}),
        ]
        for amount, title, script, extra in stages:
            progress(amount, title)
            self._run([str(python), "-s", script], {**common, **extra}, lambda line: progress(amount, line), cancel)
        # The upstream preparation scripts always suffix their shard number,
        # even when all_parts=1.  Training consumes the merged names without
        # that suffix, so materialize those canonical files explicitly.
        phoneme = exp_dir / "2-name2text.txt"
        semantic = exp_dir / "6-name2semantic.tsv"
        self._merge_feature_shards(exp_dir, "2-name2text-*.txt", phoneme, has_header=False)
        self._merge_feature_shards(exp_dir, "6-name2semantic-*.tsv", semantic, has_header=True)
        if not phoneme.is_file() or not phoneme.stat().st_size or not semantic.is_file() or not semantic.stat().st_size:
            raise RuntimeError("训练特征生成不完整")
        feature_manifest = exp_dir / "feature-manifest.json"
        feature_manifest.write_text(json.dumps({"schema_version": 1, "profile_id": profile_id, "dataset_snapshot_id": snapshot_id, "snapshot_sha256": snapshot_sha256, "list_sha256": payload["list_sha256"], "prepared_at": utc_now(), "feature_files": {"phoneme": str(phoneme), "semantic": str(semantic)}}, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(1.0, "数据集准备完成")
        return feature_manifest

    @staticmethod
    def _merge_feature_shards(root: Path, pattern: str, destination: Path, has_header: bool) -> None:
        shards = sorted(item for item in root.glob(pattern) if item.is_file() and item != destination)
        if not shards:
            return
        merged: list[str] = []
        for index, shard in enumerate(shards):
            lines = shard.read_text(encoding="utf-8").splitlines()
            if has_header and index and lines:
                lines = lines[1:]
            merged.extend(lines)
        destination.write_text("\n".join(merged) + "\n", encoding="utf-8")

    def train(self, payload: dict, progress: Callable[[float, str], None], cancel: threading.Event) -> list[Path]:
        """Run SoVITS then GPT with pinned upstream default recipes."""
        python = self.paths.private_python
        engine = self.paths.engine_root
        exp_name = payload["experiment_name"]; profile_id = validate_id(str(payload["profile_id"]), legacy=True, field="profile_id"); snapshot_id = validate_id(str(payload["dataset_snapshot_id"]), legacy=True, field="dataset_snapshot_id"); snapshot_sha256 = validate_sha256(str(payload["snapshot_sha256"]), field="snapshot_sha256")
        feature_dir = self.paths.data_root / "training" / profile_id / snapshot_sha256 / "features"
        feature_manifest_path = feature_dir / "feature-manifest.json"
        self._validate_feature_manifest(feature_manifest_path, payload)
        training_run_id = validate_id(str(payload.get("training_run_id") or uuid4().hex), legacy=True, field="training_run_id")
        run_root = self.training_run_root(self.paths.data_root, training_run_id)
        if run_root.exists(): raise RuntimeError("训练运行 ID 已存在；开始新训练时禁止自动恢复旧运行")
        run_exp = run_root / "sovits-exp"; temp = run_root / "configs"; gpt_log_dir = run_root / "gpt-logs"; sovits_log_dir = run_exp / "logs_s2_v2ProPlus"
        self._materialize_feature_view(feature_dir, run_exp); temp.mkdir(parents=True, exist_ok=True)
        sovits_log_dir.mkdir(parents=True, exist_ok=True); gpt_log_dir.mkdir(parents=True, exist_ok=True)
        project = ensure_within(self.paths.projects_root, Path(str(payload["project_path"])))
        checkpoint_dir = ensure_within(project / "checkpoints", project / "checkpoints" / profile_id / training_run_id)
        checkpoint_dir.mkdir(parents=True, exist_ok=False)
        run_manifest = run_root / "training-run.json"
        run_value = {"training_run_id": training_run_id, "profile_id": profile_id, "dataset_snapshot_id": snapshot_id, "snapshot_sha256": snapshot_sha256, "mode": "new", "status": "running", "started_at": utc_now(), "completed_at": "", "sovits_log_dir": str(sovits_log_dir), "gpt_log_dir": str(gpt_log_dir), "checkpoint_dir": str(checkpoint_dir), "candidate_gpt_checkpoint": "", "candidate_sovits_checkpoint": ""}
        run_manifest.write_text(json.dumps(run_value, ensure_ascii=False, indent=2), encoding="utf-8")
        s2_source = engine / "GPT_SoVITS/configs/s2v2ProPlus.json"
        s2 = json.loads(s2_source.read_text(encoding="utf-8"))
        s2["train"].update({
            "batch_size": int(payload.get("sovits_batch_size", 2)), "epochs": int(payload.get("sovits_epochs", 8)),
            "pretrained_s2G": str(engine / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
            "pretrained_s2D": str(engine / "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2ProPlus.pth"),
            "if_save_latest": True, "if_save_every_weights": True, "save_every_epoch": 4,
            "gpu_numbers": "0", "fp16_run": True,
        })
        s2["model"]["version"] = "v2ProPlus"
        s2["data"]["exp_dir"] = str(run_exp); s2["s2_ckpt_dir"] = str(run_root / "sovits-tensorboard")
        s2["save_weight_dir"] = str(checkpoint_dir)
        s2["name"] = exp_name
        s2["version"] = "v2ProPlus"
        s2_config = temp / f"{exp_name}-s2.json"
        s2_config.write_text(json.dumps(s2, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(0.05, "开始 SoVITS 训练")
        self._run([str(python), "-s", "GPT_SoVITS/s2_train.py", "--config", str(s2_config)], {}, lambda line: progress(0.4, line), cancel)

        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("训练环境缺少 PyYAML") from exc
        s1_source = engine / "GPT_SoVITS/configs/s1longer-v2.yaml"
        s1 = yaml.safe_load(s1_source.read_text(encoding="utf-8"))
        s1["train"].update({
            "batch_size": int(payload.get("gpt_batch_size", 2)), "epochs": int(payload.get("gpt_epochs", 15)),
            "save_every_n_epoch": 5, "if_save_every_weights": True, "if_save_latest": True,
            "half_weights_save_dir": str(checkpoint_dir), "exp_name": exp_name,
        })
        s1["pretrained_s1"] = str(engine / "GPT_SoVITS/pretrained_models/s1v3.ckpt")
        s1["train_semantic_path"] = str(run_exp / "6-name2semantic.tsv")
        s1["train_phoneme_path"] = str(run_exp / "2-name2text.txt")
        s1["output_dir"] = str(gpt_log_dir)
        s1_config = temp / f"{exp_name}-s1.yaml"
        s1_config.write_text(yaml.safe_dump(s1, allow_unicode=True), encoding="utf-8")
        progress(0.55, "开始 GPT 训练")
        self._run([str(python), "-s", "GPT_SoVITS/s1_train.py", "--config_file", str(s1_config)], {"hz": "25hz", "_CUDA_VISIBLE_DEVICES": "0"}, lambda line: progress(0.85, line), cancel)
        sovits = self._latest_checkpoint(checkpoint_dir, ".pth")
        gpt = self._latest_checkpoint(checkpoint_dir, ".ckpt")
        if not sovits or not gpt:
            raise RuntimeError(f"本次训练没有同时生成新的 GPT 和 SoVITS 检查点：{checkpoint_dir}")
        run_value.update({"status": "completed", "completed_at": utc_now(), "candidate_gpt_checkpoint": str(gpt), "candidate_sovits_checkpoint": str(sovits)}); run_manifest.write_text(json.dumps(run_value, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(1.0, "训练完成")
        return [sovits, gpt]

    @staticmethod
    def _latest_checkpoint(run_dir: Path, suffix: str) -> Path | None:
        candidates = [item for item in run_dir.rglob(f"*{suffix}") if item.is_file() and item.stat().st_size > 0]
        return max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name)) if candidates else None

    @staticmethod
    def _validate_feature_manifest(path: Path, payload: dict) -> dict:
        if not path.is_file(): raise RuntimeError("当前训练特征属于其他数据集，请重新准备训练特征")
        value = json.loads(path.read_text(encoding="utf-8"))
        for key in ("profile_id", "dataset_snapshot_id", "snapshot_sha256", "list_sha256"):
            if str(value.get(key, "")) != str(payload.get(key, "")): raise RuntimeError("当前训练特征属于其他数据集，请重新准备训练特征")
        for file_path in value.get("feature_files", {}).values():
            item = Path(file_path)
            if not item.is_file() or not item.stat().st_size: raise RuntimeError("训练特征文件缺失或为空，请重新准备训练特征")
        return value

    @staticmethod
    def _materialize_feature_view(source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for item in source.rglob("*"):
            if item.name in {"feature-manifest.json", "runtime.list"}: continue
            target = destination / item.relative_to(source)
            if item.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try: os.link(item, target)
            except OSError: shutil.copy2(item, target)

    def _prepare_source_assets(self, payload: dict, progress: Callable[[float, str], None], cancel: threading.Event) -> Path:
        """Normalize selected SourceAssets, run the upstream slicer and local ASR."""
        profile_id = validate_id(str(payload["profile_id"]), legacy=True, field="profile_id")
        preparation_id = validate_id(str(payload.get("preparation_id") or uuid4().hex), legacy=True, field="preparation_id")
        selected = set(payload.get("source_asset_ids") or [])
        assets = [item for item in payload.get("source_assets", []) if item.get("id") in selected and item.get("enabled", True) and not item.get("duplicate_of")]
        if not assets:
            raise ValueError("没有可处理的非重复素材")
        project = ensure_within(self.paths.projects_root, Path(payload["project_path"]))
        paths = self.preparation_paths(project, profile_id, preparation_id)
        run_root = paths["run_root"]; processed = paths["normalized"]; sliced = paths["segments"]
        working_root = paths["working_root"]; asr_output = paths["asr"]
        for folder in (processed, sliced, asr_output): folder.mkdir(parents=True, exist_ok=True)
        options = dict(payload.get("processing_options", {}))
        manifest = paths["manifest"]
        manifest.write_text(json.dumps({"schema_version": 1, "preparation_id": preparation_id, "profile_id": profile_id, "source_asset_ids": sorted(selected), "processing_options": options, "created_at": utc_now(), "normalized_dir": str(processed), "segments_dir": str(sliced), "asr_list": "", "status": "running"}, ensure_ascii=False, indent=2), encoding="utf-8")
        candidates = [self.paths.data_root / "tools" / "ffmpeg.exe", self.paths.runtime_root / "env" / "Library" / "bin" / "ffmpeg.exe"]
        ffmpeg = next((item for item in candidates if item.is_file() and subprocess.run([str(item), "-version"], capture_output=True).returncode == 0), None)
        if not ffmpeg:
            raise RuntimeError("FFmpeg 尚未安装，请先修复本地引擎")
        normalized: list[str] = []
        for index, asset in enumerate(assets, 1):
            if cancel.is_set(): raise RuntimeError("任务已取消")
            source = Path(asset.get("project_path") or asset.get("original_path", ""))
            if not source.is_file(): raise FileNotFoundError(f"素材不存在：{source}")
            digest_state = hashlib.sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    if cancel.is_set(): raise RuntimeError("任务已取消")
                    digest_state.update(block)
            digest = digest_state.hexdigest()
            if digest != asset.get("sha256"): raise ValueError(f"素材哈希不一致：{source.name}")
            target = processed / f"{asset['id']}.wav"
            command = [str(ffmpeg), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "32000", "-c:a", "pcm_s16le", str(target)]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if completed.returncode or not target.is_file(): raise RuntimeError(f"音频标准化失败：{source.name}\n{completed.stderr[-500:]}")
            normalized.append(str(target)); progress(0.2 * index / len(assets), f"标准化 {index}/{len(assets)}：{source.name}")
        python = self.paths.private_python
        working = processed
        if options.get("separate_vocals"):
            separated = run_root / "separated"; separated.mkdir(parents=True, exist_ok=True)
            self._run([str(python), "-m", "local_voice_studio.uvr_cli", "--engine", str(self.paths.engine_root), "--input", str(working), "--output", str(separated)], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.24, line), cancel)
            working = separated / "vocal" if (separated / "vocal").is_dir() else separated
        if options.get("denoise"):
            denoised = run_root / "denoised"; denoised.mkdir(parents=True, exist_ok=True)
            self._run([str(python), "-s", "tools/cmd-denoise.py", "-i", str(working), "-o", str(denoised), "-p", "float16"], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.28, line), cancel)
            working = denoised
        self._run([str(python), "-s", "tools/slice_audio.py", str(working), str(sliced), "-34", "4000", "300", "10", "500", "0.9", "0.25", "0", "1"], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.35, line), cancel)
        self._run([str(python), "-X", "utf8", "-u", "-m", "local_voice_studio.asr_cli", "-i", str(sliced), "-o", str(asr_output), "-l", str(payload.get("processing_options", {}).get("language", "zh"))], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.75, line), cancel)
        lists = sorted(asr_output.glob("*.list"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not lists: raise RuntimeError("ASR 完成但未生成转写列表")
        result = {"schema_version": 1, "preparation_id": preparation_id, "profile_id": profile_id, "source_asset_ids": sorted(selected), "processing_options": options, "created_at": json.loads(manifest.read_text(encoding="utf-8"))["created_at"], "normalized_dir": str(processed), "normalized": normalized, "segments_dir": str(sliced), "asr_list": str(lists[0]), "status": "completed"}
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(1.0, "训练数据准备完成，请人工校对")
        return manifest
