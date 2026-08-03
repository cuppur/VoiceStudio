from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DatasetManifest, Job, JobKind, JobStatus, SourceAsset, VoiceProfile, utc_now
from .paths import AppPaths, ensure_within


class StudioStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        paths.ensure()
        self._init_database()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_database(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL,
                    message TEXT NOT NULL,
                    error TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    outputs TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    updated_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                "UPDATE jobs SET status = ?, message = ?, updated_at = ? WHERE status = ?",
                (JobStatus.FAILED.value, "程序上次退出时任务仍在运行，可在生成页恢复", utc_now(), JobStatus.RUNNING.value),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def create_project(self, name: str) -> Path:
        safe = "".join(char for char in name.strip() if char not in '<>:"/\\|?*').strip(" .") or "未命名项目"
        base = ensure_within(self.paths.projects_root, self.paths.projects_root / safe)
        project = base
        index = 2
        while project.exists() and (project / "project.json").exists():
            project = ensure_within(self.paths.projects_root, self.paths.projects_root / f"{safe}-{index}")
            index += 1
        for child in ("raw", "processed", "datasets", "checkpoints", "exports"):
            (project / child).mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 2, "id": project.name, "name": name, "voice_profiles": [], "source_assets": [], "dataset_snapshots": [], "created_at": utc_now()}
        self._atomic_json(project / "project.json", payload)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO projects(id,name,path,updated_at) VALUES(?,?,?,?)",
                (payload["id"], name, str(project), utc_now()),
            )
        return project

    def list_projects(self) -> list[dict[str, str]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,name,path,updated_at FROM projects ORDER BY updated_at DESC").fetchall()
        return [dict(row) for row in rows]

    def load_project(self, project: Path) -> dict[str, Any]:
        project = ensure_within(self.paths.projects_root, project)
        path = project / "project.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        migrated = self._migrate_project(value, project)
        if migrated != value:
            self._atomic_json(path, migrated)
        return migrated

    @staticmethod
    def _migrate_project(value: dict[str, Any], project: Path | None = None) -> dict[str, Any]:
        result = json.loads(json.dumps(value, ensure_ascii=False))
        if int(result.get("schema_version", 1)) < 2:
            assets = result.setdefault("source_assets", [])
            known = {item.get("sha256") for item in assets}
            for profile in result.get("voice_profiles", []):
                ids = profile.setdefault("source_asset_ids", [])
                profile.setdefault("dataset_snapshot_id", "")
                profile.setdefault("default_model_mode", "fine_tuned" if profile.get("active_sovits_checkpoint") else "zero_shot")
                for ref in profile.get("reference_assets", []):
                    digest = ref.get("sha256", "")
                    if digest in known:
                        continue
                    asset = SourceAsset(
                        profile_id=profile.get("id", ""), original_path=ref.get("path", ""),
                        project_path=ref.get("path", ""), sha256=digest,
                        duration_seconds=float(ref.get("duration_seconds", 0)),
                        quality_flags=list(ref.get("quality_flags", [])), processing_status="已迁移",
                    ).to_dict()
                    assets.append(asset); ids.append(asset["id"]); known.add(digest)
            result.setdefault("dataset_snapshots", [])
            if project and not result.get("voice_profiles"):
                from .audio import scan_audio_files
                legacy_roots = [project / "raw", project / "datasets" / "current" / "slice-input"]
                probes = scan_audio_files([item for item in legacy_roots if item.exists()])
                unique = [item for item in probes if not item.duplicate_of]
                if unique:
                    profile = VoiceProfile("已迁移声音", False)
                    migrated_assets = [SourceAsset(profile.id, item.path, item.path, item.sha256, source_kind="legacy", duration_seconds=item.duration_seconds, sample_rate=item.sample_rate, channels=item.channels, codec=item.codec, quality_flags=list(item.quality_flags), processing_status="待重新准备").to_dict() for item in unique]
                    profile.source_asset_ids = [item["id"] for item in migrated_assets]
                    result["voice_profiles"].append(profile.to_dict()); result["source_assets"].extend(migrated_assets)
            result["schema_version"] = 2
        return result

    def save_profile(self, project: Path, profile: VoiceProfile) -> None:
        project = ensure_within(self.paths.projects_root, project)
        manifest = self.load_project(project)
        profiles = manifest.setdefault("voice_profiles", [])
        replacement = profile.to_dict()
        for index, item in enumerate(profiles):
            if item.get("id") == profile.id:
                profiles[index] = replacement
                break
        else:
            profiles.append(replacement)
        self._atomic_json(project / "project.json", manifest)

    def list_profiles(self, project: Path) -> list[VoiceProfile]:
        return [VoiceProfile.from_dict(dict(item)) for item in self.load_project(project).get("voice_profiles", [])]

    def save_source_assets(self, project: Path, assets: list[SourceAsset]) -> None:
        project = ensure_within(self.paths.projects_root, project)
        manifest = self.load_project(project)
        current = {item.get("id"): item for item in manifest.setdefault("source_assets", [])}
        for asset in assets:
            asset.updated_at = utc_now()
            current[asset.id] = asset.to_dict()
        manifest["source_assets"] = list(current.values())
        self._atomic_json(project / "project.json", manifest)

    def list_source_assets(self, project: Path, profile_id: str | None = None) -> list[SourceAsset]:
        assets = [SourceAsset.from_dict(item) for item in self.load_project(project).get("source_assets", [])]
        return [item for item in assets if not profile_id or item.profile_id == profile_id]

    def save_dataset_snapshot(self, project: Path, dataset: DatasetManifest) -> Path:
        project = ensure_within(self.paths.projects_root, project)
        folder = ensure_within(project / "datasets", project / "datasets" / dataset.id)
        folder.mkdir(parents=True, exist_ok=True)
        payload = asdict(dataset)
        self._atomic_json(folder / "manifest.json", payload)
        manifest = self.load_project(project)
        snapshots = manifest.setdefault("dataset_snapshots", [])
        summary = {"id": dataset.id, "voice_profile_id": dataset.voice_profile_id, "path": str(folder / "manifest.json"), "snapshot_sha256": dataset.snapshot_sha256, "approved_seconds": dataset.approved_seconds, "created_at": dataset.created_at}
        snapshots[:] = [item for item in snapshots if item.get("id") != dataset.id]
        snapshots.append(summary)
        self._atomic_json(project / "project.json", manifest)
        return folder / "manifest.json"

    def load_dataset_snapshot(self, project: Path, snapshot_id: str) -> DatasetManifest:
        project = ensure_within(self.paths.projects_root, project)
        path = ensure_within(project / "datasets", project / "datasets" / snapshot_id / "manifest.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        return DatasetManifest.from_dict(value)

    def save_job(self, job: Job) -> None:
        job.updated_at = utc_now()
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO jobs
                (id,kind,status,progress,message,error,payload,outputs,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.id, job.kind.value, job.status.value, job.progress, job.message, job.error,
                    json.dumps(job.payload, ensure_ascii=False), json.dumps(job.outputs, ensure_ascii=False),
                    job.created_at, job.updated_at,
                ),
            )

    def list_jobs(self, limit: int = 200) -> list[Job]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        jobs: list[Job] = []
        for row in rows:
            jobs.append(Job(
                id=row["id"], kind=JobKind(row["kind"]), status=JobStatus(row["status"]),
                progress=row["progress"], message=row["message"], error=row["error"],
                payload=json.loads(row["payload"]), outputs=json.loads(row["outputs"]),
                created_at=row["created_at"], updated_at=row["updated_at"],
            ))
        return jobs

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
