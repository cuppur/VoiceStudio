from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QWidget


class VoiceSelector(QComboBox):
    voice_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.currentIndexChanged.connect(self._emit_if_allowed)

    def _emit_if_allowed(self, index: int) -> None:
        if index >= 0 and self.model().item(index).flags() & Qt.ItemIsEnabled:
            self.voice_selected.emit(self.itemData(index))

    def set_profiles(self, profiles) -> None:
        self.clear()
        for profile in profiles or []:
            if isinstance(profile, dict):
                name, identifier = profile.get("name", "未命名声音"), profile.get("id", profile.get("profile_id"))
                consent_confirmed = bool(profile.get("consent_confirmed", False))
                archived = bool(profile.get("archived", False))
            else:
                name, identifier = getattr(profile, "name", str(profile)), getattr(profile, "id", None)
                consent_confirmed = bool(getattr(profile, "consent_confirmed", False))
                archived = bool(getattr(profile, "archived", False))
            singing_status = ""
            if hasattr(profile, "singing_status"):
                try:
                    singing_status = str(profile.singing_status())
                except TypeError:
                    singing_status = str(profile.singing_status(None))
            allowed = consent_confirmed and not archived
            capability = {"ready": "歌唱模型就绪", "training": "歌唱模型训练中", "untrusted": "歌唱模型未验证", "model_missing": "歌唱模型缺失"}.get(singing_status, "歌唱模型未生成")
            label = f"{name} · {'可用' if allowed else ('已归档' if archived else '未授权')} · {capability}"
            self.addItem(label, identifier)
            index = self.count() - 1
            if not allowed:
                item = self.model().item(index)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setData("已归档声音不可用" if archived else "请先确认授权", Qt.ToolTipRole)
