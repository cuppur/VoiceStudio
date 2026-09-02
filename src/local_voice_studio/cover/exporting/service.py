"""Atomic WAV/MP3 export for registered cover assets."""
from __future__ import annotations
import json, os, re, subprocess
from pathlib import Path
from typing import Any
from ...audio import sha256_file, probe_audio
from ...paths import AppPaths, ensure_within, validate_id
from ...runtime import EngineRuntimeResolver
from ..project import CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from .manifest import ProvenanceManifestBuilder
from .models import ExportFormat, ExportRequest, OverwritePolicy
from .backend import FFmpegExportBackend

def _cancelled(cancel: Any) -> bool:
    return bool(cancel() if callable(cancel) else cancel and cancel.is_set())

class CoverExporter:
    def __init__(self, paths: AppPaths | None = None, *, backend: FFmpegExportBackend | None = None):
        self.paths = paths or AppPaths.default(); self.process = None; self.backend = backend
    def cancel(self):
        if self.backend is not None:
            self.backend.cancel()
        if self.process and self.process.poll() is None:
            if os.name == "nt": subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True)
            else: self.process.terminate()
    def export(self, project: Path, cover_id: str, *, format: str = "both", output_id: str | None = None, destination: Path | None = None, file_name: str | None = None, final_asset_id: str | None = None, existing: str | None = None, cancel: Any = None, rights_confirmed: bool | None = None, publication_rights_ack: bool = False, profile_id: str = "", model_id: str = "", mix_settings: Any = None) -> dict[str, Any]:
        try:
            request = ExportRequest(ExportFormat(format), str(file_name or ""), Path(destination or ""), OverwritePolicy(existing), bool(publication_rights_ack))
        except ValueError as exc:
            raise ValueError("导出格式或覆盖策略无效") from exc
        format = request.format.value
        existing = request.overwrite_policy.value
        if not request.publication_rights_acknowledged:
            raise PermissionError("导出前必须确认公开发布权利")
        project = ensure_within(self.paths.projects_root, Path(project)); cover = CoverProject.load(project, cover_id)
        if rights_confirmed is None: rights_confirmed = cover.rights_confirmed and cover.rights_attestation_text_hash == RIGHTS_ATTESTATION_TEXT_HASH
        if not rights_confirmed or not publication_rights_ack: raise PermissionError("导出前必须确认歌曲处理与公开发布权利")
        source = cover.get_asset(final_asset_id) if final_asset_id else cover.get_asset(role="final_mix")
        if source is None: raise ValueError("没有可导出的 final_mix 资产")
        if source.role != "final_mix" or source.content_origin != "ai_generated" or source.producer not in {"voicestudio_mixer", "ffmpeg-mixer"}:
            raise ValueError("仅允许导出由 VoiceStudio Mixer 生成的 AI final_mix 资产")
        source_path = ensure_within(cover.root, cover.root / source.relative_path)
        if not source_path.is_file() or sha256_file(source_path) != source.sha256: raise ValueError("final_mix 缺失或 Hash 不匹配")
        # Keep Unicode titles while removing Windows-invalid filename syntax.
        raw_id = str(output_id or cover.title or "cover").strip()
        output_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (file_name or raw_id)).strip(" .") or "cover"
        output_id = Path(output_id).stem
        if output_id in {".", ".."}: output_id = "cover"
        folder = Path(destination).resolve() if destination else (cover.root / "exports").resolve()
        if destination and (not folder.exists() or not folder.is_dir()): raise ValueError("导出目录不存在")
        folder.mkdir(parents=True, exist_ok=True)
        paths = [folder / f"{output_id}.wav", folder / f"{output_id}.mp3"] if format == "both" else [folder / f"{output_id}.{format}"]
        sidecar = paths[0].with_name(paths[0].stem + ".voicestudio.json")
        if existing == "reject" and any(p.exists() for p in [*paths, sidecar]): raise FileExistsError("导出文件已存在")
        ffmpeg = (Path(getattr(self.backend, "ffmpeg", "ffmpeg"))
                  if self.backend is not None
                  else EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg"))
        if not ffmpeg: raise RuntimeError("找不到受信任的 FFmpeg")
        produced = []; stagings = []
        for target in paths:
            staging = target.with_name(target.name + ".staging.wav")
            args = [str(ffmpeg), "-y", "-i", str(source_path), "-ar", "48000", "-ac", "2"]
            args += ["-c:a", "pcm_s16le" if target.suffix == ".wav" else "libmp3lame"]
            if target.suffix != ".wav": args += ["-b:a", "320k"]
            args += ["-f", "wav" if target.suffix == ".wav" else "mp3", str(staging)]
            if _cancelled(cancel): raise RuntimeError("导出已取消")
            try:
                if self.backend is not None:
                    self.backend.encode(source_path, staging, format=target.suffix.lstrip("."), cancel=cancel)
                else:
                    self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    while self.process.poll() is None:
                        if _cancelled(cancel): self.cancel(); raise RuntimeError("导出已取消")
                        if hasattr(cancel, "wait"): cancel.wait(.1)
                    if self.process.returncode: raise RuntimeError("FFmpeg 导出失败")
                # Re-open the staged file through the same trusted probe used
                # for imported audio.  FFmpeg exit code alone does not prove a
                # playable, non-empty file was published.
                ffprobe = ffmpeg.with_name("ffprobe.exe")
                if ffprobe.is_file():
                    info = probe_audio(staging, ffprobe=ffprobe, cancel=cancel)
                    if info.duration_seconds <= 0 or info.sample_rate != 48000 or info.channels != 2:
                        raise RuntimeError("导出文件格式验证失败")
                elif staging.stat().st_size <= 0:
                    raise RuntimeError("导出文件为空")
                stagings.append((staging, target)); produced.append(target)
            except Exception:
                staging.unlink(missing_ok=True)
                for prior, _ in stagings: prior.unlink(missing_ok=True)
                raise
            finally:
                self.process = None
        backups = []
        sidecar_backup = sidecar.with_name(sidecar.name + ".voicestudio-backup")
        try:
            for staging, target in stagings:
                backup = target.with_name(target.name + ".voicestudio-backup")
                if target.exists():
                    backup.unlink(missing_ok=True); target.replace(backup); backups.append((backup, target))
                staging.replace(target)
            if sidecar.exists():
                sidecar_backup.unlink(missing_ok=True); sidecar.replace(sidecar_backup)
        except Exception:
            for _, target in stagings: target.unlink(missing_ok=True)
            for backup, target in backups:
                if backup.exists(): backup.replace(target)
            if sidecar_backup.exists(): sidecar_backup.replace(sidecar)
            raise
        stored_settings = source.metadata.get("settings", {}) if isinstance(source.metadata, dict) else {}
        payload = ProvenanceManifestBuilder.build(
            asset_id=source.id, cover_id=cover.id,
            voice_profile_id=profile_id or source.metadata.get("profile_id", ""),
            singing_model_id=model_id or source.model_id,
            content_origin="ai_generated", ai_generated=True,
            rights_confirmed=True, rights_attestation_text_hash=cover.rights_attestation_text_hash,
            publication_rights_ack=True, input_asset_ids=list(source.source_asset_ids),
            inputs=list(source.source_asset_ids),
            mix_settings=getattr(mix_settings, "canonical", lambda: mix_settings)() if mix_settings is not None else stored_settings,
            outputs=[{"path": p.name, "format": p.suffix.lstrip("."), "sha256": sha256_file(p)} for p in produced],
        )
        temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"); temporary.replace(sidecar)
        except Exception:
            for target in produced: target.unlink(missing_ok=True)
            for backup, target in backups:
                if backup.exists(): backup.replace(target)
            if sidecar_backup.exists(): sidecar_backup.replace(sidecar)
            raise
        for backup, _ in backups: backup.unlink(missing_ok=True)
        sidecar_backup.unlink(missing_ok=True)
        for target in produced:
            try: owned = ensure_within(cover.root, target) == target
            except ValueError: owned = False
            if owned: cover.register_output(target, target.stem)
        return {"outputs": [str(p) for p in produced], "sidecar": str(sidecar), "provenance": payload}
