"""Project-owned final mixing service.

The service owns policy, validation orchestration, cache identity and atomic
publication. Media command construction belongs exclusively to the backend.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ...audio import probe_audio, sha256_file
from ...paths import AppPaths, ensure_within, validate_id
from ..cancellation import as_cancellation_token
from ..errors import (
    AssetValidationError,
    ConsentRequiredError,
    ModelNotReadyError,
    RenderCancelledError,
    RightsRequiredError,
)
from ..models import ContentOrigin, CoverAssetRole
from ..project import CoverAsset, CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from .backend import FFmpegMixBackend, MixBackend
from .cache import MixCacheKey
from .models import CoverMixSettings
from .validation import MixAlignmentValidator, MixValidator


def _probe_audio(*args, **kwargs):
    """Resolve through the package export so existing test seams remain valid."""
    import sys

    package = sys.modules[__package__.rsplit(".", 1)[0] + ".mixing"]
    return package.probe_audio(*args, **kwargs)


class CoverMixer:
    """Coordinate a validated mix through one injected media backend."""

    def __init__(self, paths: AppPaths | None = None, *, backend: MixBackend | None = None):
        self.paths = paths or AppPaths.default()
        self._backend = backend

    @property
    def backend(self) -> MixBackend:
        """Return the injected backend or lazily construct the trusted one."""
        if self._backend is None:
            self._backend = FFmpegMixBackend(self._ffmpeg())
        return self._backend

    def cancel(self) -> None:
        if self._backend is not None:
            self._backend.cancel()

    def _ffmpeg(self) -> Path:
        from ...runtime import EngineRuntimeResolver

        found = EngineRuntimeResolver(self.paths).resolve_private_tool("ffmpeg")
        if not found:
            raise RuntimeError("找不到受信任的 FFmpeg")
        return found

    @staticmethod
    def _load_profile_manifest(project: Path, profile_id: str) -> Mapping[str, Any]:
        try:
            manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
            return next(item for item in manifest.get("voice_profiles", []) if str(item.get("id")) == profile_id)
        except Exception as exc:
            raise ConsentRequiredError("混音必须提供已授权的声音配置") from exc

    @staticmethod
    def _validate_profile(
        project: Path,
        profile_manifest: Mapping[str, Any] | None,
        profile_id: str,
        model_id: str,
        consent_confirmed: bool | None,
    ) -> tuple[Mapping[str, Any], str]:
        profile = profile_manifest or CoverMixer._load_profile_manifest(project, profile_id)
        confirmed = bool(profile.get("consent_confirmed")) if consent_confirmed is None else bool(consent_confirmed)
        if not confirmed or not profile.get("consent_confirmed_at") or not profile.get("consent_record"):
            raise ConsentRequiredError("混音前必须确认声音授权")
        selected_model_id = model_id or str(profile.get("active_singing_model_id", ""))
        model = next((item for item in profile.get("singing_models", []) if str(item.get("id")) == selected_model_id), None)
        if not model or model.get("trust_status") != "verified":
            raise ModelNotReadyError("歌唱模型未通过验证")
        for path_key, hash_key in (("checkpoint_relative_path", "checkpoint_sha256"), ("index_relative_path", "index_sha256")):
            try:
                path = ensure_within(project, project / str(model.get(path_key, "")))
            except (OSError, ValueError) as exc:
                raise ModelNotReadyError("歌唱模型路径无效") from exc
            if not path.is_file() or not model.get(hash_key) or sha256_file(path) != str(model.get(hash_key)):
                raise ModelNotReadyError("歌唱模型文件或 Hash 无效")
        return profile, selected_model_id

    def mix(
        self,
        project: Path,
        cover_id: str,
        settings: CoverMixSettings | None = None,
        *,
        profile_manifest: Mapping[str, Any] | None = None,
        profile_id: str = "",
        model_id: str = "",
        rights_confirmed: bool | None = None,
        consent_confirmed: bool | None = None,
        cancel: Any = None,
        output_id: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        settings = settings or CoverMixSettings()
        token = as_cancellation_token(cancel)
        project = ensure_within(self.paths.projects_root, Path(project))
        cover = CoverProject.load(project, cover_id)
        validator = MixValidator(cover)
        validator.require_rights(rights_confirmed)
        profile, selected_model_id = self._validate_profile(
            project, profile_manifest, profile_id, model_id, consent_confirmed
        )
        try:
            resolved = validator.resolve_inputs(settings, probe=_probe_audio, cancel=token)
        except InterruptedError as exc:
            raise RenderCancelledError("混音已取消") from exc
        MixAlignmentValidator().validate(
            resolved,
            tolerance_ms=settings.alignment_tolerance_ms,
            hard_limit_ms=1000,
        )
        cache_key = MixCacheKey.build(resolved.inputs, settings, settings.version)
        cached = next(
            (
                asset
                for asset in reversed(cover.assets)
                if asset.role == CoverAssetRole.FINAL_MIX.value and asset.producer_version == cache_key
            ),
            None,
        )
        invalid_cached_path: Path | None = None
        if cached and not force:
            cached_path = ensure_within(cover.root, cover.root / cached.relative_path)
            if cached_path.is_file() and sha256_file(cached_path) == cached.sha256:
                return {
                    "output_path": str(cached_path),
                    "output_sha256": cached.sha256,
                    "asset_id": cached.id,
                    "cache_hit": True,
                }
            invalid_cached_path = cached_path

        output_id = validate_id(output_id or "mix-" + cache_key[:16], legacy=True, field="output_id")
        folder = ensure_within(cover.root, cover.root / "generated" / "mix")
        folder.mkdir(parents=True, exist_ok=True)
        staging = folder / (output_id + ".staging.wav")
        output = folder / (output_id + ".wav")
        if output.exists():
            if invalid_cached_path == output:
                output.unlink()
            else:
                raise FileExistsError("输出资产已存在，请显式指定新的 output_id")
        try:
            if token.is_cancelled():
                raise RenderCancelledError("混音已取消")
            self.backend.render(
                resolved.inputs,
                settings,
                staging,
                duration_seconds=resolved.duration_seconds,
                cancel=token,
            )
            if token.is_cancelled():
                raise RenderCancelledError("混音已取消")
            if not staging.is_file() or staging.stat().st_size < 44:
                raise AssetValidationError("混音未生成有效 WAV")
            try:
                rendered = _probe_audio(staging, cancel=token)
            except Exception as exc:
                if token.is_cancelled():
                    raise RenderCancelledError("混音已取消") from exc
                raise
            if rendered.sample_rate != 48000 or rendered.channels != 2 or rendered.duration_seconds <= 0:
                raise AssetValidationError("最终混音不是有效的 48 kHz 立体声 WAV")
            staging.replace(output)
            asset = CoverAsset(
                output_id,
                CoverAssetRole.FINAL_MIX,
                output.relative_to(cover.root).as_posix(),
                sha256_file(output, cancel=token),
                ContentOrigin.AI_GENERATED,
                "voicestudio_mixer",
                cache_key,
                model_id=selected_model_id,
                source_asset_ids=[item.asset_id for item in resolved.inputs],
                metadata={
                    "mixer_version": settings.version,
                    "cache_key": cache_key,
                    "settings": settings.canonical(),
                    "settings_sha256": settings.sha256(),
                    "input_asset_ids": [item.asset_id for item in resolved.inputs],
                    "profile_id": str(profile.get("id", profile_id)),
                    "model_id": selected_model_id,
                },
            )
            if cached and cached.id == asset.id:
                cover.assets = [item for item in cover.assets if item.id != cached.id]
            cover.add_asset(asset)
            return {
                "output_path": str(output),
                "output_sha256": asset.sha256,
                "asset_id": asset.id,
                "cache_hit": False,
                "provenance": asset.to_dict(),
            }
        except RenderCancelledError:
            raise
        except InterruptedError as exc:
            raise RenderCancelledError("混音已取消") from exc
        finally:
            staging.unlink(missing_ok=True)


# Name used by the application architecture brief; retained as an alias for
# callers that still import the Phase 4 ``CoverMixer`` spelling.
CoverMixService = CoverMixer
