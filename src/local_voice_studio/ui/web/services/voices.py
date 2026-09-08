"""Voice profile service: audition, rename, archive and model version switching."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ....models import VoiceProfile
from .base import WebService


class VoicesService(WebService):
    """Real voice-library operations shared by the HTML shell."""

    # ------------------------------------------------------------------ state
    def _profile(self, profile_id: str) -> VoiceProfile:
        profile = next((item for item in self.store.list_profiles(self.project) if item.id == str(profile_id)), None)
        if profile is None:
            raise ValueError("找不到该声音配置")
        return profile

    def detail(self, profile_id: str) -> dict[str, Any]:
        profile = self._profile(profile_id)
        assets = [item for item in self.store.list_source_assets(self.project, profile.id) if item.enabled and not item.duplicate_of]
        seconds = sum(float(item.duration_seconds or 0) for item in assets)
        versions: list[dict[str, Any]] = []
        for version in profile.model_versions:
            versions.append({
                "kind": "tts", "id": version.id, "name": version.name,
                "meta": f"GPT-SoVITS · {str(version.created_at)[:10]}",
                "active": version.id == profile.active_model_version_id,
                "usable": str(version.status) == "available",
            })
        for model in getattr(profile, "singing_models", []) or []:
            versions.append({
                "kind": "singing", "id": model.id, "name": str(getattr(model, "name", "歌唱模型")),
                "meta": f"RVC · {str(getattr(model, 'created_at', ''))[:10]} · {str(getattr(model, 'trust_status', ''))}",
                "active": model.id == profile.active_singing_model_id,
                "usable": str(getattr(model, "trust_status", "")) in {"verified", "active", "ready"},
            })
        return {
            "id": profile.id, "name": profile.name,
            "consent": bool(profile.consent_confirmed),
            "archived": bool(profile.archived),
            "asset_count": len(assets), "seconds": round(seconds, 1),
            "tts_ready": bool(profile.active_model_version_id),
            "singing_ready": any(item["usable"] for item in versions if item["kind"] == "singing"),
            "versions": versions,
            "preview": self.preview_path(profile.id),
            "training_state": str(profile.training_state or ""),
        }

    def preview_path(self, profile_id: str) -> str:
        profile = self._profile(profile_id)
        version = next((item for item in profile.model_versions if item.id == profile.active_model_version_id), None)
        if version:
            candidate = next((item for item in version.preview_outputs if item.lower().endswith(".wav") and Path(item).is_file()), "")
            if candidate:
                return candidate
        return next((item.path for item in profile.reference_assets if item.approved and Path(item.path).is_file()), "")

    # ---------------------------------------------------------------- actions
    def create(self, name: str, consent: bool = True) -> dict[str, Any]:
        label = str(name or "").strip()
        if not label:
            raise ValueError("声音名称不能为空")
        if not consent:
            raise ValueError("必须确认说话人本人或已获得明确授权")
        now = datetime.now(timezone.utc).isoformat()
        profile = VoiceProfile(
            name=label, consent_confirmed=True, consent_record="界面确认：本人或已获得授权",
            consent_confirmed_at=now,
        )
        self.store.save_profile(self.project, profile)
        self.notify("voices.changed", {"profile_id": profile.id})
        return self.detail(profile.id)

    def rename(self, profile_id: str, name: str) -> dict[str, Any]:
        label = str(name or "").strip()
        if not label:
            raise ValueError("声音名称不能为空")
        profile = self._profile(profile_id)
        profile.name = label
        self.store.save_profile(self.project, profile)
        self.notify("voices.changed", {"profile_id": profile.id})
        return self.detail(profile.id)

    def archive(self, profile_id: str) -> dict[str, Any]:
        profile = self._profile(profile_id)
        self.store.archive_profile(self.project, profile.id)
        self.notify("voices.changed", {"profile_id": profile.id})
        return {"archived": profile.id}

    def activate_version(self, profile_id: str, version_id: str, kind: str = "tts") -> dict[str, Any]:
        profile = self._profile(profile_id)
        if str(kind) == "tts":
            self.store.activate_model_version(self.project, profile.id, str(version_id))
        else:
            model = next((item for item in getattr(profile, "singing_models", []) or [] if item.id == str(version_id)), None)
            if model is None:
                raise ValueError("找不到该歌唱模型版本")
            if str(getattr(model, "trust_status", "")) not in {"verified", "active", "ready"}:
                raise ValueError("该歌唱模型尚未通过验证，不能启用")
            profile.active_singing_model_id = model.id
            self.store.save_profile(self.project, profile)
        self.notify("voices.changed", {"profile_id": profile.id})
        return self.detail(profile.id)

    def import_model(self, paths: list[str]) -> dict[str, Any]:
        """Register nothing silently: imported files still need training/verification."""
        files = [Path(str(item)) for item in paths or []]
        existing = [item for item in files if item.is_file()]
        if not existing:
            raise ValueError("没有可用的模型文件")
        return {
            "registered": [str(item) for item in existing],
            "message": f"已选择 {len(existing)} 个本地模型文件。新界面不会直接启用未登记的文件："
                       "请在训练流程中完成授权、版本登记与验证后再启用。",
        }
