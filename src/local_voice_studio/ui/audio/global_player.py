"""Process-wide player state shared by desktop pages."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from .preview_controller import PreviewAudioController

@dataclass
class GlobalPlayerSession:
    controller: PreviewAudioController
    project_id: str = ""
    active_take_id: str = ""
    active_side: str = ""

    def load_take_pair(self, project_id: str, take_a_id: str, take_a_path: str, take_b_id: str, take_b_path: str) -> None:
        self.controller.load_ab(take_a_path, take_b_path)
        self.project_id = str(project_id); self.active_take_id = str(take_a_id); self.active_side = "A"
        self._take_ids = {"A": str(take_a_id), "B": str(take_b_id)}

    def select_take(self, side: str) -> str:
        side = str(side).upper()
        self.controller.select_ab(side)
        if not hasattr(self, "_take_ids"):
            raise RuntimeError("尚未加载 Take A/B")
        self.active_side = side; self.active_take_id = self._take_ids[side]
        return self.active_take_id

    def play(self) -> None: self.controller.play()
    def pause(self) -> None: self.controller.pause()
    def seek(self, position_ms: int) -> None: self.controller.seek(position_ms)
