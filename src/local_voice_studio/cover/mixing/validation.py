"""Pure-ish validation seam for mixer input resolution."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ...audio import sha256_file
from ...paths import ensure_within
from ..project import CoverProject, CoverAsset
from .models import CoverMixSettings, MixInput

class MixValidator:
    """Resolve project assets before an infrastructure backend is called."""
    def __init__(self, project: CoverProject): self.project = project

    def require_rights(self) -> None:
        if not self.project.rights_confirmed:
            raise PermissionError("混音前必须确认歌曲处理权利")

    def asset(self, role: str, *, origin: str | None = None) -> CoverAsset:
        asset = self.project.get_asset(role=role)
        if asset is None:
            raise ValueError(f"缺少 {role} 资产")
        if origin and asset.content_origin != origin:
            raise ValueError(f"{role} 资产来源无效")
        path = ensure_within(self.project.root, self.project.root / asset.relative_path)
        if not path.is_file() or not asset.sha256 or sha256_file(path) != asset.sha256:
            raise ValueError(f"{role} 资产缺失或 Hash 不匹配")
        return asset

    def resolve_inputs(self, settings: CoverMixSettings, *, probe: Callable[[Path], Any] | None = None,
                       cancel: Any = None) -> list[MixInput]:
        self.require_rights()
        entries = [("instrumental", "separated", settings.instrumental_gain_db),
                   ("ai_vocal", "ai_generated", settings.ai_gain_db)]
        if settings.original_vocal_gain_db != float("-inf"):
            entries.append(("vocal", "separated", settings.original_vocal_gain_db))
        result: list[MixInput] = []
        for role, origin, gain in entries:
            if gain == float("-inf"):
                continue
            asset = self.asset(role, origin=origin)
            path = ensure_within(self.project.root, self.project.root / asset.relative_path)
            if probe is not None:
                info = probe(path)
                if float(getattr(info, "duration_seconds", 0)) <= 0:
                    raise ValueError(f"输入音频时长异常: {path.name}")
            result.append(MixInput(role, asset.id, path, asset.sha256, float(gain)))
        return result
