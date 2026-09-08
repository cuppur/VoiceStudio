"""Pure provenance manifest builder."""
from __future__ import annotations
from datetime import datetime, timezone

class ProvenanceManifestBuilder:
    @staticmethod
    def asset_lineage(cover, source):
        """Carry the registered input chain without inventing legacy settings."""
        pending = list(source.source_asset_ids)
        seen = {source.id}
        lineage = []
        while pending:
            asset_id = pending.pop(0)
            if asset_id in seen:
                continue
            seen.add(asset_id)
            asset = cover.get_asset(asset_id)
            if asset is None:
                lineage.append({"id": asset_id, "status": "missing"})
                continue
            item = asset.to_dict()
            metadata = dict(item.get("metadata", {}))
            settings = metadata.get("inference_settings")
            if isinstance(settings, dict):
                settings = dict(settings)
                if "autotune" in settings:
                    settings["pitch_smoothing"] = settings.pop("autotune")
                metadata["inference_settings"] = settings
            item["metadata"] = metadata
            lineage.append(item)
            pending.extend(asset.source_asset_ids)
        return lineage

    @staticmethod
    def build(**values):
        result = {"schema_version": 1, "generator": "VoiceStudio", "generator_version": "1", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
        result.update(values)
        return result
