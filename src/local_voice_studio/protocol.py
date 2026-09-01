from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4



COMMANDS = {"health", "load_profile", "synthesize", "prepare_dataset", "train", "separate_song", "train_singing_model", "convert_vocal", "cancel", "shutdown"}
EVENTS = {"ready", "progress", "result", "error"}


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
