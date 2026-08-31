from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QWidget


class VoiceSelector(QComboBox):
    voice_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.currentIndexChanged.connect(lambda index: self.voice_selected.emit(self.itemData(index)))

    def set_profiles(self, profiles) -> None:
        self.clear()
        for profile in profiles or []:
            if isinstance(profile, dict):
                name, identifier = profile.get("name", "未命名声音"), profile.get("id", profile.get("profile_id"))
                # Singing is not implemented by this presentation-only component.
                ready = False
            else:
                name, identifier = getattr(profile, "name", str(profile)), getattr(profile, "id", None)
                ready = False
            # Singing readiness is intentionally display-only and never inferred.
            label = f"{name} · {'可用' if ready else '未就绪'}"
            self.addItem(label, identifier)
