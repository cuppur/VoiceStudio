"""Pure provenance manifest builder."""
from __future__ import annotations
from datetime import datetime, timezone

class ProvenanceManifestBuilder:
    @staticmethod
    def build(**values):
        result = {"schema_version": 1, "generator": "VoiceStudio", "generator_version": "1", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        result.update(values)
        return result
