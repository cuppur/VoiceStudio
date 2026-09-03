"""Stable cache-key construction for mixer outputs."""
from __future__ import annotations
import hashlib, json
from enum import Enum
from typing import Any, Iterable

class MixCacheKey:
    @staticmethod
    def build(inputs: Iterable[Any], settings: Any, engine_version: str = "cover-mix-v1") -> str:
        values = []
        for item in inputs:
            if hasattr(item, "sha256"):
                role = item.role.value if isinstance(item.role, Enum) else str(item.role)
                values.append({"role": role, "asset_id": item.asset_id, "sha256": item.sha256, "gain_db": item.gain_db})
            else:
                role, asset, *rest = item
                gain = rest[-1] if rest else 0.0
                role = role.value if isinstance(role, Enum) else str(role)
                values.append({"role": role, "asset_id": asset.id, "sha256": asset.sha256, "gain_db": gain})
        payload = {"inputs": values, "settings": settings.canonical(), "engine_version": engine_version}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
