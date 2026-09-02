"""UI-independent commands for Cover workflows."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from ..mixing.models import CoverMixSettings
except ImportError:  # compatibility while older package layout is present
    from ..mixing.service import CoverMixSettings
from ..exporting.models import ExportFormat, OverwritePolicy


@dataclass(frozen=True)
class PrepareSeparationCommand:
    project_id: str
    cover_id: str
    source_relative_path: str
    source_sha256: str
    mode: str = "uvr5"

    def to_payload(self) -> dict[str, Any]:
        return {"project_path": self.project_id, "cover_id": self.cover_id,
                "source_relative_path": self.source_relative_path,
                "source_sha256": self.source_sha256, "mode": self.mode}

    to_worker_payload = to_payload


@dataclass(frozen=True)
class PrepareAIVocalCommand:
    project_id: str
    cover_id: str
    profile_id: str
    singing_model_id: str
    pitch_shift: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {"project_path": self.project_id, "cover_id": self.cover_id,
                "profile_id": self.profile_id, "singing_model_id": self.singing_model_id,
                "pitch_shift": self.pitch_shift}

    to_worker_payload = to_payload


@dataclass(frozen=True)
class PrepareRenderCommand:
    project_id: str
    cover_id: str
    profile_id: str
    singing_model_id: str
    mix: CoverMixSettings = field(default_factory=CoverMixSettings)

    def to_payload(self) -> dict[str, Any]:
        return {"project_path": self.project_id, "cover_id": self.cover_id,
                "profile_id": self.profile_id, "singing_model_id": self.singing_model_id,
                "mix_settings": self.mix.canonical()}

    to_worker_payload = to_payload


@dataclass(frozen=True)
class ExportCoverCommand:
    project_id: str
    cover_id: str
    final_asset_id: str
    format: ExportFormat | str
    file_name: str
    destination: Path
    existing_policy: OverwritePolicy | str = OverwritePolicy.REJECT
    publication_rights_acknowledged: bool = False

    def to_payload(self) -> dict[str, Any]:
        export_format = self.format.value if isinstance(self.format, ExportFormat) else str(self.format)
        policy = self.existing_policy.value if isinstance(self.existing_policy, OverwritePolicy) else str(self.existing_policy)
        return {"project_path": self.project_id, "cover_id": self.cover_id,
                "final_asset_id": self.final_asset_id, "format": export_format,
                "file_name": self.file_name, "destination": str(self.destination),
                "existing_policy": policy,
                "publication_rights_acknowledged": self.publication_rights_acknowledged}

    to_worker_payload = to_payload


# Public product vocabulary aliases.  The ``Prepare*`` names remain for
# compatibility with early Phase 4 callers while new code can use the command
# names from the architecture contract.
SeparateSongCommand = PrepareSeparationCommand
ConvertVocalCommand = PrepareAIVocalCommand
RenderCoverCommand = PrepareRenderCommand
