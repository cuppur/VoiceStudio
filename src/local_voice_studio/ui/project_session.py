from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..models import utc_now
from ..paths import ensure_within
from ..storage import StudioStore


class ProjectSession(QObject):
    """Remember and switch projects without adding a fifth navigation page."""

    project_changed = Signal(object)
    projects_changed = Signal()
    LAST_PROJECT_KEY = "ui.last_project"
    RECENT_PROJECTS_KEY = "ui.recent_projects"

    def __init__(self, store: StudioStore, parent: QObject | None = None):
        super().__init__(parent)
        self.store = store
        self.paths = store.paths
        self._current = self._restore_or_create()
        self._remember(self._current)

    @property
    def current(self) -> Path:
        return self._current

    def display_name(self, project: Path | None = None) -> str:
        target = (project or self._current).resolve()
        try:
            return str(self.store.load_project(target).get("name") or target.name)
        except (OSError, ValueError, json.JSONDecodeError):
            return target.name

    def projects(self) -> list[dict[str, str]]:
        known: dict[str, dict[str, str]] = {}
        for item in self.store.list_projects():
            path = Path(item["path"]).resolve()
            if (path / "project.json").is_file():
                known[str(path).lower()] = {**item, "path": str(path)}
        recent = self.store.get_setting(self.RECENT_PROJECTS_KEY, [])
        order = [str(Path(value).resolve()).lower() for value in recent] if isinstance(recent, list) else []
        return sorted(known.values(), key=lambda item: order.index(item["path"].lower()) if item["path"].lower() in order else len(order))

    def activate(self, project: Path) -> Path:
        target = ensure_within(self.paths.projects_root, project)
        self.store.load_project(target)
        if target != self._current:
            self._current = target
            self._remember(target)
            self.project_changed.emit(target)
        else:
            self._remember(target)
        return target

    def create(self, name: str) -> Path:
        project = self.store.create_project(name.strip() or "未命名项目")
        self.projects_changed.emit()
        return self.activate(project)

    def open_existing(self, project: Path) -> Path:
        target = ensure_within(self.paths.projects_root, project)
        value = self.store.load_project(target)
        with self.store._connect() as db:  # UI compatibility bridge until storage exposes registration.
            db.execute(
                "INSERT OR REPLACE INTO projects(id,name,path,updated_at) VALUES(?,?,?,?)",
                (str(value.get("id") or target.name), str(value.get("name") or target.name), str(target), utc_now()),
            )
        self.projects_changed.emit()
        return self.activate(target)

    def rename(self, name: str) -> str:
        clean = name.strip()
        if not clean:
            raise ValueError("项目名称不能为空")
        value = self.store.load_project(self._current)
        value["name"] = clean
        self.store._atomic_json(self._current / "project.json", value)
        with self.store._connect() as db:  # Keep the existing project index in sync.
            db.execute("UPDATE projects SET name = ?, updated_at = ? WHERE path = ?", (clean, utc_now(), str(self._current)))
        self.projects_changed.emit()
        return clean

    def _restore_or_create(self) -> Path:
        saved = self.store.get_setting(self.LAST_PROJECT_KEY, "")
        candidates = [Path(saved)] if saved else []
        candidates.extend(Path(item["path"]) for item in self.store.list_projects())
        for candidate in candidates:
            try:
                target = ensure_within(self.paths.projects_root, candidate)
                self.store.load_project(target)
                return target
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return self.store.create_project("默认项目")

    def _remember(self, project: Path) -> None:
        target = str(project.resolve())
        recent = [target]
        saved = self.store.get_setting(self.RECENT_PROJECTS_KEY, [])
        for item in saved if isinstance(saved, list) else []:
            try:
                value = str(Path(item).resolve())
            except (OSError, TypeError, ValueError):
                continue
            if value.lower() != target.lower() and (Path(value) / "project.json").is_file():
                recent.append(value)
        self.store.set_setting(self.LAST_PROJECT_KEY, target)
        self.store.set_setting(self.RECENT_PROJECTS_KEY, recent[:8])
