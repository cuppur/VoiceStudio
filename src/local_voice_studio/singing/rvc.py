from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import queue
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .base import EngineReadiness
from .verification import VerificationResult, verify_inference_output


@dataclass(frozen=True)
class RVCConfig:
    engine_root: Path
    python: Path
    env_root: Path
    commit: str = ""
    hubert_sha256: str = ""
    rmvpe_sha256: str = ""
    pretrained_sha256: tuple[str, ...] = ()
    torch_version: str = "2.7.1+cu128"


class RVCReadiness:
    def __init__(self, config: RVCConfig):
        self.config = config

    def check(self) -> EngineReadiness:
        c = self.config
        errors: list[str] = []
        details: dict[str, Any] = {"engine_root": str(c.engine_root), "commit": c.commit, "torch_version": c.torch_version, "isolated": c.env_root.resolve() != Path(os.environ.get("LOCAL_VOICE_STUDIO_ENV", "")).resolve() if os.environ.get("LOCAL_VOICE_STUDIO_ENV") else True}
        if not c.engine_root.is_dir(): errors.append("RVC 源码目录不存在")
        required_scripts = ("train/preprocess.py", "train/dataset/extract_f0.py", "train/dataset/extract_hubert_feature.py", "train/train.py", "train/train_index.py")
        missing_scripts = [p for p in required_scripts if not (c.engine_root / p).is_file()]
        if missing_scripts: errors.append("RVC 训练脚本缺失: " + ", ".join(missing_scripts))
        if not (c.engine_root / "infer/vc/modules.py").is_file(): errors.append("RVC 转换模块缺失: infer/vc/modules.py")
        marker = c.engine_root / ".pinned-commit"
        if not c.commit: errors.append("RVC commit 未固定")
        elif not marker.is_file() or marker.read_text(encoding="utf-8").strip() != c.commit: errors.append("RVC 源码 commit marker 不匹配")
        if not c.python.is_file(): errors.append("RVC 隔离 Python 不存在")
        if not c.env_root.is_dir(): errors.append("RVC 隔离环境不存在")
        if c.python.is_file():
            try:
                probe = subprocess.run([str(c.python), "-c", "import torch; print(torch.__version__); print(torch.cuda.is_available())"], capture_output=True, text=True, timeout=15)
                lines = probe.stdout.strip().splitlines()
                if probe.returncode != 0 or not lines or lines[0].strip() != c.torch_version:
                    errors.append(f"RVC PyTorch 版本不匹配（需要 {c.torch_version}）")
                elif len(lines) < 2 or lines[1].strip().lower() != "true":
                    errors.append("RVC CUDA 不可用")
            except (OSError, subprocess.SubprocessError):
                errors.append("RVC 运行时探针失败")
        for label, digest, candidates in (
            ("HuBERT", c.hubert_sha256, (c.engine_root / "assets/hubert_base/hubert_base.pt", c.engine_root / "assets/hubert_base/pytorch_model.bin")),
            ("RMVPE", c.rmvpe_sha256, (c.engine_root / "assets/rmvpe/rmvpe.pt", c.engine_root / "rmvpe.pt")),
        ):
            existing = next((p for p in candidates if p.is_file() and p.stat().st_size), None)
            if existing is None: errors.append(f"{label} 权重不存在")
            elif digest and _sha256(existing) != digest.lower(): errors.append(f"{label} 权重 SHA-256 不匹配")
        pre = list((c.engine_root / "assets/pretrained_v2").glob("*.pth")) if (c.engine_root / "assets/pretrained_v2").is_dir() else []
        if not pre: errors.append("RVC v2 pretrained 权重不存在")
        for expected in c.pretrained_sha256:
            if not any(_sha256(p) == expected.lower() for p in pre): errors.append(f"RVC pretrained SHA-256 缺失: {expected}")
        details["pretrained_files"] = [p.name for p in pre]
        details["missing_scripts"] = missing_scripts
        return EngineReadiness(not errors, tuple(errors), details)

    __call__ = check


class RVCEngine:
    """Adapter around the upstream scripts. It never imports the ML runtime."""
    def __init__(self, config: RVCConfig, runner: Callable[..., int] | None = None):
        self.config = config
        self.process: subprocess.Popen | None = None
        self._runner = runner

    def readiness(self) -> EngineReadiness:
        return RVCReadiness(self.config).check()

    def _run(self, args: list[str], cwd: Path | None = None, cancel: Any = None) -> None:
        if self._runner is not None:
            code = self._runner(args, cwd=cwd or self.config.engine_root, cancel=cancel)
            if code: raise RuntimeError(f"RVC 子进程退出码 {code}")
            return
        env = os.environ.copy(); env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
        env["PATH"] = os.pathsep.join([str(self.config.env_root), str(self.config.env_root / "Scripts"), env.get("PATH", "")])
        product_src = Path(__file__).resolve().parents[2]
        extra_paths = [p for p in (os.environ.get("LOCAL_VOICE_STUDIO_RVC_PYTHONPATH", "").split(os.pathsep)) if p]
        search_roots = [str(self.config.engine_root)]
        if cwd is not None and cwd.resolve() != self.config.engine_root.resolve():
            search_roots.append(str(cwd))
        env["PYTHONPATH"] = os.pathsep.join(extra_paths + search_roots + [str(product_src)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        self.process = subprocess.Popen(args, cwd=str(cwd or self.config.engine_root), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        output_tail: list[str] = []
        lines: queue.Queue[str | None] = queue.Queue()
        def _drain() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                lines.put(line.rstrip())
            lines.put(None)
        reader = threading.Thread(target=_drain, name="rvc-output", daemon=True)
        reader.start()
        try:
            while True:
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    line = ""
                if line is None:
                    if self.process.poll() is not None:
                        break
                elif line:
                    output_tail.append(line)
                    if len(output_tail) > 20: output_tail.pop(0)
                if len(output_tail) > 20: output_tail.pop(0)
                if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                    self.cancel(); raise RuntimeError("任务已取消")
                if self.process.poll() is not None and lines.empty():
                    break
            code = self.process.wait()
        finally: self.process = None
        if code: raise RuntimeError(f"RVC 子进程退出码 {code}: {' | '.join(output_tail[-5:])}")

    def cancel(self) -> None:
        p = self.process
        if p and p.poll() is None:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
            else: os.killpg(os.getpgid(p.pid), signal.SIGTERM)

    def _prepare_training_files(self, experiment: Path, sample_rate: str, version: str) -> None:
        feature_dim = "256" if version == "v1" else "768"
        gt_root = experiment / "0_gt_wavs"; feature_root = experiment / f"3_feature{feature_dim}"; f0_root = experiment / "2a_f0"; f0nsf_root = experiment / "2b-f0nsf"
        names = ({path.stem for path in gt_root.glob("*.wav")} & {path.stem for path in feature_root.glob("*.npy")} & {path.name.removesuffix(".wav.npy") for path in f0_root.glob("*.wav.npy")} & {path.name.removesuffix(".wav.npy") for path in f0nsf_root.glob("*.wav.npy")})
        if not names:
            raise RuntimeError("RVC 没有可用于训练的有效音频")
        def escaped(path: Path) -> str: return str(path.resolve()).replace("\\", "\\\\")
        lines = [f"{escaped(gt_root / (name + '.wav'))}|{escaped(feature_root / (name + '.npy'))}|{escaped(f0_root / (name + '.wav.npy'))}|{escaped(f0nsf_root / (name + '.wav.npy'))}|0" for name in sorted(names)]
        (experiment / "filelist.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        template = self.config.engine_root / "configs" / version / f"{sample_rate}.json"
        if not template.is_file():
            raise RuntimeError(f"RVC 训练配置不存在: {template}")
        shutil.copy2(template, experiment / "config.json")

    def _build_index(self, experiment: Path, version: str, n_processes: str, cancel: Any = None) -> Path:
        """Run upstream index creation with a short, Windows-safe experiment name.

        Upstream embeds ``exp_name`` in output filenames, so passing our
        absolute project path produces invalid Windows filenames.  A
        project-owned temporary workspace exposes the extracted features
        under a short name; the resulting index is then normalized back to
        ``model.index`` inside the training run directory.
        """
        safe_name = "voicestudio_" + experiment.name.removesuffix(".staging")
        index_experiment = self.config.engine_root / "logs" / safe_name
        feature_name = "3_feature256" if version == "v1" else "3_feature768"
        source_features = experiment / feature_name
        linked_features = index_experiment / feature_name
        if index_experiment.exists():
            shutil.rmtree(index_experiment)
        linked_features.mkdir(parents=True)
        try:
            for source in source_features.glob("*.npy"):
                target = linked_features / source.name
                try: os.link(source, target)
                except OSError: shutil.copy2(source, target)
            if not any(linked_features.iterdir()):
                raise RuntimeError("RVC 特征文件不存在")
            self._run([str(self.config.python), "-m", "train.train_index", safe_name, version, str(experiment), n_processes], self.config.engine_root, cancel)
            candidates = sorted(
                [*experiment.glob("*added*.index"), *experiment.glob("*.index"), *index_experiment.glob("*added*.index"), *index_experiment.glob("*.index")],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if not candidates:
                raise RuntimeError("RVC 训练未生成 index")
            final = experiment / "model.index"
            if candidates[0].resolve() != final.resolve():
                shutil.copy2(candidates[0], final)
            return final
        finally:
            shutil.rmtree(index_experiment, ignore_errors=True)

    def train(self, payload: Mapping[str, Any], cancel: Any = None) -> Sequence[Path]:
        root = self.config.engine_root; exp = str(payload["experiment_dir"]); sr = str(payload.get("sample_rate", "48k")); version = str(payload.get("version", "v2"))
        sr_hz = {"32k": "32000", "40k": "40000", "48k": "48000"}.get(sr.lower(), sr)
        # Upstream 2.3 exposes these as webui callbacks, backed by these scripts.
        n_processes = str(payload.get("n_processes", 4))
        preparation_cmds = [
            [str(self.config.python), "-m", "train.preprocess", str(payload["dataset_dir"]), sr_hz, n_processes, exp, "False", "3.0"],
            [str(self.config.python), "-m", "train.dataset.extract_f0", "cuda", "1", "0", "0", exp, "True"],
            [str(self.config.python), "-m", "train.dataset.extract_hubert_feature", "cuda:0", "1", "0", "0", exp, version, "True"],
        ]
        for cmd in preparation_cmds: self._run(cmd, root, cancel)
        self._prepare_training_files(Path(exp), sr, version)
        training_cmd = [str(self.config.python), "-m", "train.train", "-e", exp, "-sr", sr, "-f0", "1", "-bs", str(payload.get("batch_size", 4)), "-te", str(payload.get("epochs", 20)), "-se", str(payload.get("save_every", 5)), "-pg", str(payload.get("pretrained_g", root / "assets/pretrained_v2/f0G48k.pth")), "-pd", str(payload.get("pretrained_d", root / "assets/pretrained_v2/f0D48k.pth")), "-l", "1", "-c", "0", "-sw", "0", "-v", version]
        self._run(training_cmd, root, cancel)
        self._build_index(Path(exp), version, n_processes, cancel)
        discovered = [p for p in (Path(exp).rglob("*") if Path(exp).is_dir() else []) if p.is_file() and p.suffix.lower() in {".pth", ".index"}]
        raw = next((p for p in discovered if p.name.startswith("G_") and p.name.endswith(".pth")), None)
        if raw is not None and bool(payload.get("prepare_checkpoint", True)):
            prepared = Path(exp) / "model.pth"
            bridge = Path(__file__).with_name("rvc_bridge.py")
            self._run([str(self.config.python), str(bridge), "--prepare-checkpoint", "--model", str(raw), "--output", str(prepared), "--sample-rate", sr, "--version", version], root, cancel)
            discovered.append(prepared)
        return tuple(discovered or (Path(p) for p in payload.get("outputs", [])))

    def convert(self, payload: Mapping[str, Any], cancel: Any = None) -> Path:
        out = Path(str(payload["output_path"]))
        bridge = Path(__file__).with_name("rvc_bridge.py")
        cmd = [str(self.config.python), str(bridge), "--input", str(payload["input_path"]), "--model", str(payload["model_path"]), "--index", str(payload.get("index_path", "")), "--pitch", str(payload.get("pitch_shift", payload.get("transpose", 0))), "--output", str(out)]
        self._run(cmd, self.config.engine_root, cancel)
        if not out.is_file() or not out.stat().st_size: raise RuntimeError("RVC 转换未生成输出音频")
        return out

    def verify_model(self, checkpoint: Path, index: Path | None = None, test_input: Path | None = None, test_output: Path | None = None) -> VerificationResult:
        result = VerificationResult(False, [])
        if index is None or not index.is_file():
            result.errors.append("RVC 模型验证必须提供 index 文件")
            return result
        args = [str(self.config.python), str(Path(__file__).with_name("rvc_bridge.py")), "--verify-model", "--model", str(checkpoint)]
        args.extend(["--index", str(index)])
        if test_input is None or test_output is None:
            try:
                self._run(args, self.config.engine_root)
                return VerificationResult(True, [], {"restricted_load": True, "index_verified": True})
            except Exception as exc:
                return VerificationResult(False, [f"RVC 安全加载验证失败: {exc}"])
        input_result = verify_inference_output(test_input, test_input)
        if not input_result.ok:
            return VerificationResult(False, input_result.errors, input_result.details)
        args.extend(["--test-input", str(test_input), "--test-output", str(test_output)])
        try:
            self._run(args, self.config.engine_root)
            return verify_inference_output(test_input, test_output, source_sha256=input_result.details.get("input_sha256", ""))
        except Exception as exc:
            return VerificationResult(False, [f"RVC 真实推理验证失败: {exc}"])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()
