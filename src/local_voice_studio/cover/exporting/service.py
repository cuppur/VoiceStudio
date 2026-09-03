"""Atomic export transaction for registered cover assets.

The service owns validation, staging, publication, rollback, and provenance.
Audio encoding is deliberately delegated to :class:`FFmpegExportBackend` (or
an injected backend in tests); process management does not live in this layer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...audio import sha256_file
from ...paths import AppPaths, ensure_within
from ...runtime import EngineRuntimeResolver
from ..cancellation import as_cancellation_token
from ..errors import AssetValidationError, CoverError, ExportConflictError, RightsRequiredError
from ..models import ContentOrigin, CoverAssetRole
from ..project import CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from .backend import ExportBackend, FFmpegExportBackend
from .manifest import ProvenanceManifestBuilder
from .models import ExportFormat, ExportRequest, OverwritePolicy
from .validation import ExportOutputValidator


class CoverExporter:
    """Export a validated AI final mix through one injected backend."""

    def __init__(self, paths: AppPaths | None = None, *, backend: ExportBackend | None = None, probe=None):
        self.paths = paths or AppPaths.default()
        self.backend = backend
        self.validator = ExportOutputValidator(probe)

    def _backend(self) -> ExportBackend:
        if self.backend is None:
            # Compatibility construction for callers that do not own a
            # composition root. Encoding still goes exclusively through the
            # formal backend; this service never builds an FFmpeg command.
            ffmpeg = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("找不到受信任的 FFmpeg")
            self.backend = FFmpegExportBackend(ffmpeg)
        return self.backend

    def cancel(self) -> None:
        """Cancel the active backend operation, if one exists."""
        backend = self.backend
        if backend is not None:
            backend.cancel()

    @staticmethod
    def _cancel_error() -> CoverError:
        return CoverError("导出已取消", code="cover.export_cancelled", recoverable=True)

    @staticmethod
    def _safe_output_id(cover: CoverProject, file_name: str | None, output_id: str | None) -> str:
        # Keep Unicode titles while removing Windows-invalid filename syntax.
        raw_id = str(output_id or cover.title or "cover").strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (file_name or raw_id)).strip(" .") or "cover"
        value = Path(value).stem
        return "cover" if value in {".", "..", ""} else value

    @staticmethod
    def _formats(value: ExportFormat | str) -> tuple[str, ...]:
        try:
            export_format = value.value if isinstance(value, ExportFormat) else ExportFormat(str(value)).value
        except ValueError as exc:
            raise ValueError("导出格式无效") from exc
        return ("wav", "mp3") if export_format == "both" else (export_format,)

    @staticmethod
    def _cleanup(paths: list[Path]) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def export(
        self,
        project: Path,
        cover_id: str,
        *,
        format: ExportFormat | str = ExportFormat.BOTH,
        output_id: str | None = None,
        destination: Path | None = None,
        file_name: str | None = None,
        final_asset_id: str | None = None,
        existing: OverwritePolicy | str = OverwritePolicy.REJECT,
        cancel: Any = None,
        rights_confirmed: bool | None = None,
        publication_rights_ack: bool = False,
        profile_id: str = "",
        model_id: str = "",
        mix_settings: Any = None,
    ) -> dict[str, Any]:
        try:
            overwrite_policy = existing.value if isinstance(existing, OverwritePolicy) else OverwritePolicy(str(existing)).value
        except ValueError as exc:
            raise ValueError("覆盖策略无效") from exc
        formats = self._formats(format)
        request = ExportRequest(
            ExportFormat.BOTH if len(formats) == 2 else ExportFormat(formats[0]),
            str(file_name or ""),
            Path(destination or ""),
            OverwritePolicy(overwrite_policy),
            bool(publication_rights_ack),
        )
        token = as_cancellation_token(cancel)
        if token.is_cancelled():
            raise self._cancel_error()
        if not request.publication_rights_acknowledged:
            raise RightsRequiredError("导出前必须确认公开发布权利")

        project = ensure_within(self.paths.projects_root, Path(project))
        cover = CoverProject.load(project, cover_id)
        if rights_confirmed is None:
            rights_confirmed = cover.rights_confirmed and cover.rights_attestation_text_hash == RIGHTS_ATTESTATION_TEXT_HASH
        if not rights_confirmed or not publication_rights_ack:
            raise RightsRequiredError("导出前必须确认歌曲处理与公开发布权利")

        source = cover.get_asset(final_asset_id) if final_asset_id else cover.get_asset(role=CoverAssetRole.FINAL_MIX.value)
        if source is None:
            raise AssetValidationError("没有可导出的 final_mix 资产")
        try:
            source_role = CoverAssetRole.parse(source.role)
            source_origin = ContentOrigin(source.content_origin)
        except (TypeError, ValueError) as exc:
            raise AssetValidationError("final_mix 资产角色或来源无效") from exc
        if source_role is not CoverAssetRole.FINAL_MIX or source_origin is not ContentOrigin.AI_GENERATED:
            raise AssetValidationError("仅允许导出 AI 生成的 final_mix 资产")
        if source.producer not in {"voicestudio_mixer", "ffmpeg-mixer"}:
            raise AssetValidationError("final_mix 资产不是由 VoiceStudio Mixer 生成")
        source_path = ensure_within(cover.root, cover.root / source.relative_path)
        if not source_path.is_file() or not source.sha256 or sha256_file(source_path) != source.sha256:
            raise AssetValidationError("final_mix 缺失或 Hash 不匹配")
        try:
            source_probe = self.validator.probe(source_path, cancel=token)
        except InterruptedError as exc:
            raise self._cancel_error() from exc
        except Exception as exc:
            raise AssetValidationError("无法读取 final_mix 音频") from exc
        source_duration = float(
            source_probe.get("duration_seconds", 0) if isinstance(source_probe, dict)
            else getattr(source_probe, "duration_seconds", 0)
        )
        if source_duration <= 0:
            raise AssetValidationError("final_mix 时长无效")

        output_name = self._safe_output_id(cover, file_name, output_id)
        folder = Path(destination).resolve() if destination else (cover.root / "exports").resolve()
        if destination and (not folder.exists() or not folder.is_dir()):
            raise ValueError("导出目录不存在")
        folder.mkdir(parents=True, exist_ok=True)
        targets = [folder / f"{output_name}.{suffix}" for suffix in formats]
        sidecar = folder / f"{output_name}.voicestudio.json"
        all_destinations = [*targets, sidecar]
        if overwrite_policy == OverwritePolicy.REJECT.value and any(path.exists() for path in all_destinations):
            raise ExportConflictError("导出文件已存在")

        backend = self._backend()
        staging_paths = [target.with_name(target.name + ".staging") for target in targets]
        sidecar_staging = sidecar.with_name(sidecar.name + ".staging")
        staged: list[Path] = []
        try:
            for target, staging in zip(targets, staging_paths):
                if token.is_cancelled():
                    raise self._cancel_error()
                try:
                    backend.encode(source_path, staging, format=target.suffix.lstrip("."), cancel=token)
                except InterruptedError as exc:
                    raise self._cancel_error() from exc
                except RuntimeError as exc:
                    if token.is_cancelled():
                        raise self._cancel_error() from exc
                    raise
                if not staging.is_file() or staging.stat().st_size <= 0:
                    raise AssetValidationError(f"导出未生成有效的 {target.suffix.lstrip('.').upper()} 文件")
                try:
                    self.validator.validate(
                        staging,
                        expected_format=target.suffix.lstrip("."),
                        source_duration_seconds=source_duration,
                        cancel=token,
                    )
                except InterruptedError as exc:
                    raise self._cancel_error() from exc
                staged.append(staging)

            stored_settings = source.metadata.get("settings", {}) if isinstance(source.metadata, dict) else {}
            payload = ProvenanceManifestBuilder.build(
                asset_id=source.id,
                cover_id=cover.id,
                voice_profile_id=profile_id or source.metadata.get("profile_id", ""),
                singing_model_id=model_id or source.model_id,
                content_origin=ContentOrigin.AI_GENERATED.value,
                ai_generated=True,
                rights_confirmed=True,
                rights_attestation_text_hash=cover.rights_attestation_text_hash,
                publication_rights_ack=True,
                input_asset_ids=list(source.source_asset_ids),
                inputs=list(source.source_asset_ids),
                mix_settings=getattr(mix_settings, "canonical", lambda: mix_settings)() if mix_settings is not None else stored_settings,
                outputs=[{"path": target.name, "format": target.suffix.lstrip("."), "sha256": sha256_file(staging)} for target, staging in zip(targets, staged)],
            )
        except BaseException:
            self._cleanup([*staging_paths, sidecar_staging])
            raise

        backups: list[tuple[Path, Path]] = []
        published: list[Path] = []
        backup_paths = [path.with_name(path.name + ".voicestudio-backup") for path in all_destinations]
        try:
            for target, backup in zip(all_destinations, backup_paths):
                if target.exists():
                    backup.unlink(missing_ok=True)
                    target.replace(backup)
                    backups.append((backup, target))
            for staging, target in zip(staging_paths, targets):
                staging.replace(target)
                published.append(target)
            # Write the provenance as part of the same transaction.  If this
            # fails after WAV/MP3 publication, the rollback below restores all
            # pre-existing outputs and removes the newly published files.
            sidecar_staging.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            sidecar_staging.replace(sidecar)
            published.append(sidecar)
        except BaseException:
            self._cleanup([*staging_paths, sidecar_staging, *published])
            for backup, target in reversed(backups):
                if backup.exists():
                    backup.replace(target)
            raise

        for backup, _ in backups:
            backup.unlink(missing_ok=True)
        self._cleanup([*staging_paths, sidecar_staging, *backup_paths])
        for target in targets:
            try:
                if ensure_within(cover.root, target) == target:
                    cover.register_output(target, target.stem)
            except ValueError:
                pass
        return {"outputs": [str(path) for path in targets], "sidecar": str(sidecar), "provenance": payload}
