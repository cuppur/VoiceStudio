"""Project-owned, deterministic final mixing."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ...audio import probe_audio, sha256_file
from ...paths import AppPaths, ensure_within, validate_id
from ..project import CoverAsset, CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from .models import CoverMixSettings, MixInput
from .backend import FFmpegMixBackend
from .cache import MixCacheKey
from .validation import MixValidator

def _probe_audio(*args, **kwargs):
    # Resolve through the public package so legacy monkeypatches remain valid.
    import sys
    return sys.modules[__package__.rsplit(".", 1)[0] + ".mixing"].probe_audio(*args, **kwargs)


def _cancelled(cancel: Any) -> bool:
    return bool(cancel() if callable(cancel) else cancel and cancel.is_set())


class CoverMixer:
    def __init__(self, paths: AppPaths | None = None, *, backend: FFmpegMixBackend | None = None):
        self.paths = paths or AppPaths.default()
        self.backend = backend
        self.process: subprocess.Popen | None = None

    def cancel(self) -> None:
        if self.backend is not None:
            self.backend.cancel()
        if self.process and self.process.poll() is None:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
            else: self.process.terminate()

    def _ffmpeg(self) -> Path:
        from ...runtime import EngineRuntimeResolver
        found = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg")
        if not found: raise RuntimeError("找不到受信任的 FFmpeg")
        return found

    def mix(self, project: Path, cover_id: str, settings: CoverMixSettings | None = None, *, profile_manifest: Mapping[str, Any] | None = None, profile_id: str = "", model_id: str = "", rights_confirmed: bool | None = None, consent_confirmed: bool | None = None, cancel: Any = None, output_id: str | None = None, force: bool = False) -> dict[str, Any]:
        settings = settings or CoverMixSettings()
        project = ensure_within(self.paths.projects_root, Path(project)); cover = CoverProject.load(project, cover_id)
        MixValidator(cover).require_rights()
        if rights_confirmed is None: rights_confirmed = cover.rights_confirmed and cover.rights_attestation_text_hash == RIGHTS_ATTESTATION_TEXT_HASH
        if not rights_confirmed: raise PermissionError("混音前必须确认歌曲处理权利")
        if profile_manifest is None:
            try:
                manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
                profile_manifest = next(x for x in manifest.get("voice_profiles", []) if str(x.get("id")) == profile_id)
            except Exception as exc: raise PermissionError("混音必须提供已授权的声音配置") from exc
        if consent_confirmed is None: consent_confirmed = bool(profile_manifest.get("consent_confirmed"))
        if not consent_confirmed or not profile_manifest.get("consent_confirmed_at") or not profile_manifest.get("consent_record"): raise PermissionError("混音前必须确认声音授权")
        selected_model_id = model_id or str(profile_manifest.get("active_singing_model_id", ""))
        model = next((x for x in profile_manifest.get("singing_models", []) if str(x.get("id")) == selected_model_id), None)
        if not model or model.get("trust_status") != "verified": raise PermissionError("歌唱模型未通过验证")
        for path_key, hash_key in (("checkpoint_relative_path", "checkpoint_sha256"), ("index_relative_path", "index_sha256")):
            p = ensure_within(project, project / str(model.get(path_key, "")))
            if not p.is_file() or sha256_file(p) != str(model.get(hash_key, "")): raise PermissionError("歌唱模型文件或 Hash 无效")
        inst = cover.get_asset(role="instrumental"); ai = cover.get_asset(role="ai_vocal"); vocal = cover.get_asset(role="vocal")
        if not ai or ai.content_origin != "ai_generated": raise ValueError("必须存在 AI 生成的人声资产")
        if not inst or inst.content_origin != "separated": raise ValueError("必须存在已分离的伴奏资产")
        chosen = [("instrumental", inst, settings.instrumental_gain_db), ("ai_vocal", ai, settings.ai_gain_db)]
        if settings.original_vocal_gain_db != float("-inf"):
            if not vocal or vocal.content_origin != "separated": raise ValueError("原唱人声必须是已分离资产")
            chosen.append(("vocal", vocal, settings.original_vocal_gain_db))
        selected = [(role, asset, gain) for role, asset, gain in chosen if gain != float("-inf")]
        inputs: list[tuple[str, CoverAsset, Path, float]] = []
        probes: dict[Path, Any] = {}
        for role, asset, gain in selected:
            path = ensure_within(cover.root, cover.root / asset.relative_path)
            if not path.is_file() or not asset.sha256 or sha256_file(path) != asset.sha256: raise ValueError(f"输入资产缺失或 Hash 不匹配: {role}")
            probe = _probe_audio(path, cancel=cancel)
            if probe.duration_seconds <= 1: raise ValueError(f"输入音频时长异常: {path.name}")
            probes[path] = probe
            inputs.append((role, asset, path, gain))
        input_duration = max(probes[item[2]].duration_seconds for item in inputs)
        if abs(probes[inputs[0][2]].duration_seconds - probes[inputs[1][2]].duration_seconds) > 1.0: raise ValueError("AI 人声与伴奏时长差异超过 1 秒")
        # Cache identity is independent of provenance and includes each input
        # hash, semantic role, gain and mixer engine version.
        cache_inputs = [MixInput(r, a.id, path, a.sha256, gain) for r, a, path, gain in inputs]
        cache_key = MixCacheKey.build(cache_inputs, settings, settings.version)
        cached = next((a for a in reversed(cover.assets) if a.role == "final_mix" and a.producer_version == cache_key), None)
        invalid_cached_path: Path | None = None
        if cached and not force:
            cp = ensure_within(cover.root, cover.root / cached.relative_path)
            if cp.is_file() and sha256_file(cp) == cached.sha256: return {"output_path": str(cp), "output_sha256": cached.sha256, "asset_id": cached.id, "cache_hit": True}
            invalid_cached_path = cp
        output_id = validate_id(output_id or "mix-" + cache_key[:16], legacy=True, field="output_id")
        folder = ensure_within(cover.root, cover.root / "generated" / "mix"); folder.mkdir(parents=True, exist_ok=True)
        staging = folder / (output_id + ".staging.wav"); output = folder / (output_id + ".wav")
        if output.exists():
            if invalid_cached_path == output: output.unlink()
            else: raise FileExistsError("输出资产已存在，请显式指定新的 output_id")
        ffmpeg = (Path(getattr(self.backend, "ffmpeg", "ffmpeg"))
                  if self.backend is not None else self._ffmpeg())
        args = [str(ffmpeg), "-y"]
        for _, _, path, _ in inputs: args += ["-i", str(path)]
        filters = []
        for i, (role, _, _, gain) in enumerate(inputs): filters.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={gain}dB[a{i}]")
        labels = "".join(f"[a{i}]" for i in range(len(inputs))); chain = labels + f"amix=inputs={len(inputs)}:duration=longest:normalize={'1' if settings.normalize else '0'},volume={settings.master_gain_db}dB"
        if settings.limiter: chain += ",alimiter=limit=0.95"
        if settings.fade_in_ms: chain += f",afade=t=in:st=0:d={settings.fade_in_ms/1000:g}"
        if settings.fade_out_ms:
            fade_duration = settings.fade_out_ms / 1000
            chain += f",afade=t=out:st={max(0.0, input_duration - fade_duration):g}:d={fade_duration:g}"
        filters.append(chain + ",aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo[out]")
        args += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_s16le", "-f", "wav", str(staging)]
        try:
            if _cancelled(cancel): raise RuntimeError("混音已取消")
            if self.backend is not None:
                self.backend.render(cache_inputs, settings, staging, cancel=cancel)
            else:
                self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                while self.process.poll() is None:
                    if _cancelled(cancel): self.cancel(); raise RuntimeError("混音已取消")
                    if cancel is not None and hasattr(cancel, "wait"): cancel.wait(.1)
                if self.process.returncode: raise RuntimeError("FFmpeg 混音失败")
            if not staging.is_file() or staging.stat().st_size < 44: raise RuntimeError("混音未生成有效 WAV")
            rendered = _probe_audio(staging, cancel=cancel)
            if rendered.sample_rate != 48000 or rendered.channels != 2 or rendered.duration_seconds <= 1:
                raise RuntimeError("最终混音不是有效的 48 kHz 立体声 WAV")
            staging.replace(output)
            asset = CoverAsset(output_id, "final_mix", output.relative_to(cover.root).as_posix(), sha256_file(output), "ai_generated", "voicestudio_mixer", cache_key, model_id=selected_model_id, source_asset_ids=[a.id for _, a, _, _ in inputs], metadata={"mixer_version": settings.version, "cache_key": cache_key, "settings": settings.canonical(), "settings_sha256": settings.sha256(), "input_asset_ids": [a.id for _, a, _, _ in inputs], "profile_id": str(profile_manifest.get("id", profile_id)), "model_id": selected_model_id})
            if cached and cached.id == asset.id:
                cover.assets = [item for item in cover.assets if item.id != cached.id]
            cover.add_asset(asset)
            return {"output_path": str(output), "output_sha256": asset.sha256, "asset_id": asset.id, "cache_hit": False, "provenance": asset.to_dict()}
        finally:
            self.process = None; staging.unlink(missing_ok=True)
