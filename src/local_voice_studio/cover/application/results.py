"""Stable result objects returned by Cover application operations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class OperationResult:
    """A transport-neutral result; ``payload`` remains JSONL-compatible."""
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return dict(self.payload)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OperationResult":
        return cls(dict(payload))


@dataclass(frozen=True)
class CoverStateResult:
    cover_id: str
    title: str
    separation_status: str
    rights_confirmed: bool
    assets: tuple[dict[str, Any], ...] = ()
    final_asset_id: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {"cover_id": self.cover_id, "title": self.title,
                "separation_status": self.separation_status,
                "rights_confirmed": self.rights_confirmed,
                "assets": [dict(x) for x in self.assets],
                "final_asset_id": self.final_asset_id}


@dataclass(frozen=True)
class SeparateSongResult(OperationResult):
    pass


@dataclass(frozen=True)
class ConvertVocalResult(OperationResult):
    pass


@dataclass(frozen=True)
class RenderCoverResult(OperationResult):
    pass


@dataclass(frozen=True)
class ExportCoverResult(OperationResult):
    pass
