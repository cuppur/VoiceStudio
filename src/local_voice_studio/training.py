from __future__ import annotations

import json
import os
import subprocess
import threading
import hashlib
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .paths import AppPaths


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
            + [process_env.get("PATH", "")]
        )
        process_env["PYTHONUTF8"] = "1"
        process_env["PYTHONIOENCODING"] = "utf-8"
        process_env.setdefault("HF_HOME", str(self.paths.models_root / "huggingface"))
        process_env.setdefault("MODELSCOPE_CACHE", str(self.paths.models_root / "modelscope"))
        process_env.setdefault("TORCH_HOME", str(self.paths.models_root / "torch"))
        existing_pythonpath = process_env.get("PYTHONPATH", "")
        process_env["PYTHONPATH"] = os.pathsep.join(
            [str(self.paths.engine_root), *( [existing_pythonpath] if existing_pythonpath else [] )]
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
        list_path = Path(payload["list_path"]).resolve()
        wav_dir = Path(payload["wav_dir"]).resolve()
        exp_dir = self.paths.data_root / "training" / exp_name
        exp_dir.mkdir(parents=True, exist_ok=True)
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
        progress(1.0, "数据集准备完成")
        return exp_dir

    def train(self, payload: dict, progress: Callable[[float, str], None], cancel: threading.Event) -> list[Path]:
        """Run SoVITS then GPT with pinned upstream default recipes."""
        python = self.paths.private_python
        engine = self.paths.engine_root
        exp_name = payload["experiment_name"]
        exp_dir = self.paths.data_root / "training" / exp_name
        checkpoint_root = Path(payload.get("checkpoint_dir") or (exp_dir / "weights")).resolve()
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        if not (exp_dir / "2-name2text.txt").exists() or not (exp_dir / "6-name2semantic.tsv").exists():
            raise RuntimeError("训练特征尚未准备完成")
        # Every training invocation gets a new directory. Old or partially
        # generated checkpoints can therefore never be mistaken for this run.
        checkpoint_dir = checkpoint_root / f"run-{uuid4().hex}"
        checkpoint_dir.mkdir(parents=True, exist_ok=False)
        temp = self.paths.data_root / "training" / "configs"
        temp.mkdir(parents=True, exist_ok=True)
        s2_source = engine / "GPT_SoVITS/configs/s2v2ProPlus.json"
        s2 = json.loads(s2_source.read_text(encoding="utf-8"))
        s2["train"].update({
            "batch_size": int(payload.get("sovits_batch_size", 8)), "epochs": int(payload.get("sovits_epochs", 8)),
            "pretrained_s2G": str(engine / "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
            "pretrained_s2D": str(engine / "GPT_SoVITS/pretrained_models/v2Pro/s2Dv2ProPlus.pth"),
            "if_save_latest": True, "if_save_every_weights": True, "save_every_epoch": 4,
            "gpu_numbers": "0", "fp16_run": True,
        })
        s2["model"]["version"] = "v2ProPlus"
        s2["data"]["exp_dir"] = s2["s2_ckpt_dir"] = str(exp_dir)
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
            "batch_size": int(payload.get("gpt_batch_size", 8)), "epochs": int(payload.get("gpt_epochs", 15)),
            "save_every_n_epoch": 5, "if_save_every_weights": True, "if_save_latest": True,
            "half_weights_save_dir": str(checkpoint_dir), "exp_name": exp_name,
        })
        s1["pretrained_s1"] = str(engine / "GPT_SoVITS/pretrained_models/s1v3.ckpt")
        s1["train_semantic_path"] = str(exp_dir / "6-name2semantic.tsv")
        s1["train_phoneme_path"] = str(exp_dir / "2-name2text.txt")
        s1["output_dir"] = str(exp_dir / "logs_s1_v2ProPlus")
        s1_config = temp / f"{exp_name}-s1.yaml"
        s1_config.write_text(yaml.safe_dump(s1, allow_unicode=True), encoding="utf-8")
        progress(0.55, "开始 GPT 训练")
        self._run([str(python), "-s", "GPT_SoVITS/s1_train.py", "--config_file", str(s1_config)], {"hz": "25hz", "_CUDA_VISIBLE_DEVICES": "0"}, lambda line: progress(0.85, line), cancel)
        sovits = self._latest_checkpoint(checkpoint_dir, ".pth")
        gpt = self._latest_checkpoint(checkpoint_dir, ".ckpt")
        if not sovits or not gpt:
            raise RuntimeError(f"本次训练没有同时生成新的 GPT 和 SoVITS 检查点：{checkpoint_dir}")
        progress(1.0, "训练完成")
        return [sovits, gpt]

    @staticmethod
    def _latest_checkpoint(run_dir: Path, suffix: str) -> Path | None:
        candidates = [item for item in run_dir.rglob(f"*{suffix}") if item.is_file() and item.stat().st_size > 0]
        return max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name)) if candidates else None

    def _prepare_source_assets(self, payload: dict, progress: Callable[[float, str], None], cancel: threading.Event) -> Path:
        """Normalize selected SourceAssets, run the upstream slicer and local ASR."""
        profile_id = str(payload["profile_id"])
        selected = set(payload.get("source_asset_ids") or [])
        assets = [item for item in payload.get("source_assets", []) if item.get("id") in selected and item.get("enabled", True) and not item.get("duplicate_of")]
        if not assets:
            raise ValueError("没有可处理的非重复素材")
        project = Path(payload["project_path"]).resolve()
        processed = project / "processed" / profile_id / "normalized"
        sliced = project / "processed" / profile_id / "segments"
        asr_output = project / "datasets" / "working" / profile_id / "asr"
        for folder in (processed, sliced, asr_output): folder.mkdir(parents=True, exist_ok=True)
        candidates = [self.paths.data_root / "tools" / "ffmpeg.exe", self.paths.runtime_root / "env" / "Library" / "bin" / "ffmpeg.exe"]
        ffmpeg = next((item for item in candidates if item.is_file() and subprocess.run([str(item), "-version"], capture_output=True).returncode == 0), None)
        if not ffmpeg:
            raise RuntimeError("FFmpeg 尚未安装，请先修复本地引擎")
        normalized: list[str] = []
        for index, asset in enumerate(assets, 1):
            if cancel.is_set(): raise RuntimeError("任务已取消")
            source = Path(asset.get("project_path") or asset.get("original_path", ""))
            if not source.is_file(): raise FileNotFoundError(f"素材不存在：{source}")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if digest != asset.get("sha256"): raise ValueError(f"素材哈希不一致：{source.name}")
            target = processed / f"{asset['id']}.wav"
            command = [str(ffmpeg), "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "32000", "-c:a", "pcm_s16le", str(target)]
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if completed.returncode or not target.is_file(): raise RuntimeError(f"音频标准化失败：{source.name}\n{completed.stderr[-500:]}")
            normalized.append(str(target)); progress(0.2 * index / len(assets), f"标准化 {index}/{len(assets)}：{source.name}")
        python = self.paths.private_python
        options = payload.get("processing_options", {}); working = processed
        if options.get("separate_vocals"):
            separated = processed.parent / "separated"; separated.mkdir(parents=True, exist_ok=True)
            self._run([str(python), "-m", "local_voice_studio.uvr_cli", "--engine", str(self.paths.engine_root), "--input", str(working), "--output", str(separated)], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.24, line), cancel)
            working = separated / "vocal" if (separated / "vocal").is_dir() else separated
        if options.get("denoise"):
            denoised = processed.parent / "denoised"; denoised.mkdir(parents=True, exist_ok=True)
            self._run([str(python), "-s", "tools/cmd-denoise.py", "-i", str(working), "-o", str(denoised), "-p", "float16"], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.28, line), cancel)
            working = denoised
        self._run([str(python), "-s", "tools/slice_audio.py", str(working), str(sliced), "-34", "4000", "300", "10", "500", "0.9", "0.25", "0", "1"], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.35, line), cancel)
        self._run([str(python), "-X", "utf8", "-u", "-m", "local_voice_studio.asr_cli", "-i", str(sliced), "-o", str(asr_output), "-l", str(payload.get("processing_options", {}).get("language", "zh"))], {"_CUDA_VISIBLE_DEVICES": "0", "is_half": "True"}, lambda line: progress(0.75, line), cancel)
        lists = sorted(asr_output.glob("*.list"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not lists: raise RuntimeError("ASR 完成但未生成转写列表")
        result = {"schema_version": 2, "profile_id": profile_id, "source_asset_ids": sorted(selected), "normalized": normalized, "segments_dir": str(sliced), "asr_list": str(lists[0])}
        manifest = asr_output.parent / "preparation.json"
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(1.0, "训练数据准备完成，请人工校对")
        return manifest
