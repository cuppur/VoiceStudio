"""Cover workflow application service.

This service turns business identifiers and user intent into trusted worker
commands. It deliberately does not send IPC itself, so it can be used by Qt,
CLI, and tests alike.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...models import VoiceProfile
from ...audio import sha256_file
from ...paths import AppPaths, ensure_within, validate_id, validate_sha256
from ...storage import StudioStore
try:
    from ..mixing.models import CoverMixSettings
except ImportError:  # compatibility while older package layout is present
    from ..mixing.service import CoverMixSettings
from ..project import CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from ..models import CoverAssetRole, ContentOrigin
from ..errors import (AssetValidationError, ConsentRequiredError,
                      ModelNotReadyError, RightsRequiredError)
from .commands import ExportCoverCommand, PrepareAIVocalCommand, PrepareRenderCommand, PrepareSeparationCommand
from .results import CoverStateResult


class CoverApplicationService:
    """Orchestrate Cover use-cases while keeping UI free of domain payloads."""

    def __init__(self, project: Path, *, paths: AppPaths | None = None, store: StudioStore | None = None):
        self.paths = paths or AppPaths.default()
        self.project = ensure_within(self.paths.projects_root, Path(project))
        self.store = store or StudioStore(self.paths)

    def _cover(self, cover_id: str) -> CoverProject:
        validate_id(cover_id, legacy=True, field="cover_id")
        return CoverProject.load(self.project, cover_id)

    def _profile(self, profile_id: str) -> VoiceProfile:
        validate_id(profile_id, legacy=True, field="profile_id")
        profile = next((p for p in self.store.list_profiles(self.project) if p.id == profile_id and not p.archived), None)
        if not profile:
            raise PermissionError("目标声音不存在或已归档")
        if not profile.consent_confirmed or not profile.consent_record or not profile.consent_confirmed_at:
            raise ConsentRequiredError("该声音尚未确认本人或授权使用")
        return profile

    def get_state(self, cover_id: str) -> CoverStateResult:
        cover = self._cover(cover_id)
        final = cover.get_asset(role="final_mix")
        return CoverStateResult(cover.id, cover.title, cover.separation_status,
                                bool(cover.rights_confirmed),
                                tuple(asset.to_dict() for asset in cover.assets),
                                final.id if final else "")

    def prepare_separation(self, cover_id: str, *, mode: str = "uvr5") -> PrepareSeparationCommand:
        cover = self._cover(cover_id)
        if not cover.rights_confirmed or cover.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH:
            raise RightsRequiredError("开始分离前必须确认歌曲处理与使用权利声明")
        if mode != "uvr5":
            raise ValueError("当前仅支持 UVR5 分离")
        validate_sha256(cover.source_sha256, field="source_sha256")
        source = ensure_within(cover.root, cover.root / cover.source_relative_path)
        if not source.is_file():
            raise AssetValidationError("歌曲源文件不存在")
        if sha256_file(source) != cover.source_sha256:
            raise AssetValidationError("歌曲源文件 SHA-256 不匹配")
        return PrepareSeparationCommand(str(self.project), cover.id, cover.source_relative_path, cover.source_sha256, mode)

    create_separation_command = prepare_separation

    def prepare_ai_vocal(self, cover_id: str, profile_id: str, *, pitch_shift: int = 0) -> PrepareAIVocalCommand:
        cover = self._cover(cover_id); profile = self._profile(profile_id)
        if not cover.rights_confirmed or cover.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH:
            raise RightsRequiredError("开始 AI 翻唱前必须确认歌曲处理权利")
        model_id = profile.active_singing_model_id
        model = next((m for m in profile.singing_models if m.id == model_id), None)
        if not model or model.trust_status != "verified":
            raise ModelNotReadyError("歌唱模型未通过验证")
        return PrepareAIVocalCommand(str(self.project), cover.id, profile.id, model.id, int(pitch_shift))

    create_ai_vocal_command = prepare_ai_vocal

    def prepare_render(self, cover_id: str, profile_id: str, mix_state: CoverMixSettings | dict[str, Any] | None = None) -> PrepareRenderCommand:
        cover = self._cover(cover_id); profile = self._profile(profile_id)
        if not cover.rights_confirmed or cover.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH:
            raise RightsRequiredError("生成最终混音前必须确认歌曲处理权利")
        ai = cover.get_asset(role="ai_vocal")
        instrumental = cover.get_asset(role="instrumental")
        if not ai or ai.role != CoverAssetRole.AI_VOCAL or ai.content_origin != ContentOrigin.AI_GENERATED:
            raise AssetValidationError("生成最终混音前必须存在 AI 人声")
        if not instrumental or instrumental.role != CoverAssetRole.INSTRUMENTAL or instrumental.content_origin != ContentOrigin.SEPARATED:
            raise AssetValidationError("生成最终混音前必须存在已分离伴奏")
        model_id = profile.active_singing_model_id
        model = next((m for m in profile.singing_models if m.id == model_id), None)
        if not model or model.trust_status != "verified": raise ModelNotReadyError("歌唱模型未通过验证")
        settings = mix_state if isinstance(mix_state, CoverMixSettings) else CoverMixSettings(**(mix_state or {}))
        return PrepareRenderCommand(str(self.project), cover.id, profile.id, model.id, settings)

    create_render_command = prepare_render

    def prepare_export(self, cover_id: str, *, final_asset_id: str, format: str, file_name: str,
                       destination: Path, existing_policy: str = "reject",
                       publication_rights_acknowledged: bool = False) -> ExportCoverCommand:
        cover = self._cover(cover_id)
        if not cover.rights_confirmed or cover.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH:
            raise RightsRequiredError("导出前必须确认歌曲处理权利")
        asset = cover.get_asset(final_asset_id)
        if not asset or asset.role != CoverAssetRole.FINAL_MIX or asset.content_origin != ContentOrigin.AI_GENERATED:
            raise AssetValidationError("只能导出已生成的最终混音")
        if format not in {"wav", "mp3", "both"}: raise AssetValidationError("不支持的导出格式")
        if existing_policy not in {"reject", "replace"}: raise AssetValidationError("不支持的覆盖策略")
        if not publication_rights_acknowledged: raise RightsRequiredError("导出前必须确认发布权利提醒")
        return ExportCoverCommand(str(self.project), cover.id, asset.id, format, file_name,
                                  Path(destination), existing_policy, True)

    create_export_command = prepare_export
