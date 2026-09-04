from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4



COMMANDS = {"health", "load_profile", "synthesize", "prepare_dataset", "train", "separate_song", "cleanup_vocal", "suggest_transpose", "train_singing_model", "convert_vocal", "render_cover", "export_cover", "transcribe_lyrics", "cancel", "shutdown"}
EVENTS = {"ready", "progress", "result", "error"}

# Product commands use positive allowlists.  Paths and hashes which can be
# derived from the owning project never cross the UI/worker trust boundary.
PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "train_singing_model": frozenset({
        "project_path", "profile_id", "source_asset_ids", "training_run_id", "engine",
    }),
    "convert_vocal": frozenset({
        "project_path", "cover_id", "profile_id", "singing_model_id", "pitch_shift", "inference_settings",
    }),
    "render_cover": frozenset({
        "project_path", "cover_id", "profile_id", "singing_model_id", "mix_settings",
    }),
    "export_cover": frozenset({
        "project_path", "cover_id", "final_asset_id", "format", "file_name",
        "destination", "existing_policy", "publication_rights_acknowledged",
    }),
    "separate_song": frozenset({
        "project_path", "cover_id", "source_relative_path", "source_sha256", "mode",
    }),
    "cleanup_vocal": frozenset({"project_path", "cover_id", "cleanup_settings"}),
    "suggest_transpose": frozenset({"project_path", "cover_id", "profile_id", "singing_model_id"}),
    "transcribe_lyrics": frozenset({"project_path", "cover_id", "language"}),
}


def validate_payload(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown fields for security-sensitive product commands."""
    allowed = PAYLOAD_FIELDS.get(command)
    if allowed is None:
        return dict(payload)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{command} 包含未知字段: {', '.join(unknown)}")
    return dict(payload)


@dataclass
class Message:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)

    def encode(self) -> bytes:
        return (json.dumps({"id": self.id, "type": self.type, "payload": self.payload}, ensure_ascii=False) + "\n").encode("utf-8")

    @classmethod
    def decode(cls, value: str | bytes) -> "Message":
        if isinstance(value, bytes):
            value = value.decode("utf-8-sig")
        item = json.loads(value.lstrip("\ufeff"))
        if not isinstance(item, dict) or "type" not in item:
            raise ValueError("消息必须是包含 type 的对象")
        if not isinstance(item.get("payload", {}), dict):
            raise ValueError("payload 必须是对象")
        return cls(id=str(item.get("id") or uuid4().hex), type=str(item["type"]), payload=item.get("payload", {}))
