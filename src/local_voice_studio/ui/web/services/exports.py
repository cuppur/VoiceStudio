"""Export centre and cache maintenance service."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ....paths import ensure_within
from .base import WebService


class ExportsService(WebService):
    """Real export listing plus safe cache cleanup."""

    def list(self) -> dict[str, Any]:
        rows = self.snapshot_rows()
        return {
            "items": rows,
            "project_exports": str(ensure_within(self.paths.projects_root, self.project / "exports")),
            "cache_root": str(self.paths.cache_root),
            "counts": {
                "all": len(rows),
                "cover": len([item for item in rows if item["kind"] == "cover"]),
                "tts": len([item for item in rows if item["kind"] == "tts"]),
            },
        }

    def snapshot_rows(self) -> list[dict[str, Any]]:
        from .data import StudioSnapshot
        return StudioSnapshot(self.paths, self.store, self.project).exports()

    def clean_cache(self) -> dict[str, Any]:
        active = [command for command in getattr(self.client, "pending", {}).values() if command not in {"health", "load_profile"}]
        if active:
            raise ValueError("有任务正在处理音频，请等待结束后再清理缓存")
        cache = self.paths.cache_root
        removed: list[str] = []
        for name in ("preview", "waveforms"):
            target = ensure_within(cache, cache / name)
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(name)
        cache.mkdir(parents=True, exist_ok=True)
        message = "已清理试听与波形缓存" if removed else "没有需要清理的缓存"
        self.notify("exports.changed", {"removed": removed})
        return {"message": message, "removed": removed, "data": self.list()}

    def open_folder(self, target: str = "") -> dict[str, Any]:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        path = Path(target).resolve() if str(target).strip() else ensure_within(self.paths.projects_root, self.project / "exports")
        if not path.is_dir():
            path = path.parent
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return {"message": "已打开导出目录"}
