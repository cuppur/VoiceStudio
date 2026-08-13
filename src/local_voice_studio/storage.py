from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .audio import sha256_file
from .models import DatasetDraft, DatasetManifest, GenerationRecord, Job, JobKind, JobStatus, ModelVersion, SourceAsset, TrainingWorkflow, VoiceProfile, WorkflowStatus, dataset_snapshot_sha256, utc_now
from .paths import AppPaths, ensure_within, validate_id, validate_sha256


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
                (JobStatus.INTERRUPTED.value, "程序上次退出时任务中断，可从对应工作流继续", utc_now(), JobStatus.RUNNING.value),
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
        for child in ("raw", "processed", "datasets", "checkpoints", "exports", "workflows", "drafts"):
            (project / child).mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 4, "project_uid": uuid4().hex, "id": project.name, "name": name, "voice_profiles": [], "source_assets": [], "dataset_snapshots": [], "workflows": [], "generation_records": [], "created_at": utc_now()}
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
        for profile in result.get("voice_profiles", []):
            profile.setdefault("current_workflow_id", ""); profile.setdefault("last_workflow_id", ""); profile.setdefault("active_model_version_id", ""); profile.setdefault("model_versions", []); profile.setdefault("archived", False)
            if profile.get("active_gpt_checkpoint") and profile.get("active_sovits_checkpoint") and not profile["model_versions"]:
                version = ModelVersion(name="已迁移版本", gpt_checkpoint=profile["active_gpt_checkpoint"], sovits_checkpoint=profile["active_sovits_checkpoint"], status="active")
                profile["model_versions"] = [version.to_dict()]; profile["active_model_version_id"] = version.id
        result.setdefault("workflows", [])
        # Keep the legacy pure-data helper compatible; real project loads pass
        # a project path and always migrate atomically to schema v4.
        if project is None:
            result["schema_version"] = 3
            return result
        if int(result.get("schema_version", 1)) < 4:
            result.setdefault("project_uid", uuid4().hex)
            result.setdefault("generation_records", [])
            for profile in result.get("voice_profiles", []):
                for version in profile.get("model_versions", []):
                    version.setdefault("gpt_sha256", "")
                    version.setdefault("sovits_sha256", "")
                    version.setdefault("origin", "legacy-local")
                    version.setdefault("trust_status", "unverified")
                    if project and not version["gpt_sha256"] and not version["sovits_sha256"]:
                        try:
                            checkpoint_root = ensure_within(project, project / "checkpoints")
                            gpt = ensure_within(checkpoint_root, Path(version.get("gpt_checkpoint", "")))
                            sovits = ensure_within(checkpoint_root, Path(version.get("sovits_checkpoint", "")))
                            if not gpt.is_file() or not sovits.is_file(): raise FileNotFoundError
                            version["gpt_sha256"] = sha256_file(gpt)
                            version["sovits_sha256"] = sha256_file(sovits)
                            # The GUI process intentionally does not ship
                            # torch.  The private worker upgrades this state
                            # only after restricted deserialization succeeds.
                            version["trust_status"] = "legacy-pending"
                            if profile.get("active_model_version_id") == version.get("id"):
                                profile["active_gpt_sha256"] = version["gpt_sha256"]
                                profile["active_sovits_sha256"] = version["sovits_sha256"]
                                profile["active_model_trust_status"] = version["trust_status"]
                        except Exception:
                            version["trust_status"] = "legacy-pending"
        result["schema_version"] = 4
        if project:
            for child in ("workflows", "drafts"): (project / child).mkdir(parents=True, exist_ok=True)
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

    def archive_profile(self, project: Path, profile_id: str) -> None:
        profile = next((item for item in self.list_profiles(project) if item.id == profile_id), None)
        if not profile: raise KeyError(profile_id)
        profile.archived = True; self.save_profile(project, profile)

    def activate_model_version(self, project: Path, profile_id: str, version_id: str) -> VoiceProfile:
        validate_id(profile_id, legacy=True, field="profile_id")
        validate_id(version_id, legacy=True, field="version_id")
        profile = next((item for item in self.list_profiles(project) if item.id == profile_id), None)
        if not profile: raise KeyError(profile_id)
        version = next((item for item in profile.model_versions if item.id == version_id), None)
        if version:
            self.verify_model_version(project, version)
        if not version or not Path(version.gpt_checkpoint).is_file() or not Path(version.sovits_checkpoint).is_file(): raise FileNotFoundError("所选声音版本不可用")
        for item in profile.model_versions:
            if item.status == "active": item.status = "available"
        version.status = "active"; profile.active_model_version_id = version.id; profile.active_gpt_checkpoint = version.gpt_checkpoint; profile.active_sovits_checkpoint = version.sovits_checkpoint; profile.active_gpt_sha256 = version.gpt_sha256; profile.active_sovits_sha256 = version.sovits_sha256; profile.active_model_trust_status = version.trust_status; profile.default_model_mode = "fine_tuned"; self.save_profile(project, profile); return profile

    @staticmethod
    def verify_model_version(project: Path, version: ModelVersion) -> None:
        checkpoints = ensure_within(project, project / "checkpoints")
        gpt = ensure_within(checkpoints, Path(version.gpt_checkpoint))
        sovits = ensure_within(checkpoints, Path(version.sovits_checkpoint))
        if not gpt.is_file() or not sovits.is_file():
            raise FileNotFoundError("所选声音版本的模型文件缺失")
        validate_sha256(version.gpt_sha256, field="gpt_sha256")
        validate_sha256(version.sovits_sha256, field="sovits_sha256")
        if sha256_file(gpt) != version.gpt_sha256 or sha256_file(sovits) != version.sovits_sha256:
            raise ValueError("模型文件哈希不一致，已拒绝加载")
        if version.trust_status not in {"verified", "trusted-local"}:
            raise ValueError("模型版本尚未通过安全验证")

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

    def remove_source_assets(self, project: Path, asset_ids: set[str], delete_project_copies: bool = True) -> list[SourceAsset]:
        """Remove imported assets without ever touching their original files."""
        project = ensure_within(self.paths.projects_root, project)
        manifest = self.load_project(project)
        removed = [SourceAsset.from_dict(item) for item in manifest.get("source_assets", []) if item.get("id") in asset_ids]
        if not removed:
            return []
        manifest["source_assets"] = [item for item in manifest.get("source_assets", []) if item.get("id") not in asset_ids]
        for profile in manifest.get("voice_profiles", []):
            profile["source_asset_ids"] = [item for item in profile.get("source_asset_ids", []) if item not in asset_ids]
        self._atomic_json(project / "project.json", manifest)
        if delete_project_copies:
            remaining_paths = {str(Path(item.get("project_path", "")).resolve()) for item in manifest["source_assets"] if item.get("project_path")}
            reference_paths = {str(Path(item.get("path", "")).resolve()) for profile in manifest.get("voice_profiles", []) for item in profile.get("reference_assets", []) if item.get("path")}
            for asset in removed:
                try:
                    copied = ensure_within(project / "raw", Path(asset.project_path))
                    if str(copied.resolve()) not in remaining_paths | reference_paths and copied.is_file():
                        copied.unlink()
                except (OSError, ValueError):
                    pass
        return removed

    def save_workflow(self, project: Path, workflow: TrainingWorkflow) -> Path:
        project = ensure_within(self.paths.projects_root, project); workflow.updated_at = utc_now(); folder = ensure_within(project, project / "workflows"); folder.mkdir(parents=True, exist_ok=True); path = folder / f"{workflow.id}.json"; self._atomic_json(path, workflow.to_dict())
        manifest = self.load_project(project); summaries = manifest.setdefault("workflows", []); summary = {"id": workflow.id, "voice_profile_id": workflow.voice_profile_id, "stage": workflow.stage.value, "status": workflow.status.value, "updated_at": workflow.updated_at, "path": path.relative_to(project).as_posix()}; summaries[:] = [item for item in summaries if item.get("id") != workflow.id]; summaries.append(summary); self._atomic_json(project / "project.json", manifest); return path

    def load_workflow(self, project: Path, workflow_id: str) -> TrainingWorkflow:
        validate_id(workflow_id, legacy=True, field="workflow_id")
        project = ensure_within(self.paths.projects_root, project); path = ensure_within(project / "workflows", project / "workflows" / f"{workflow_id}.json"); return TrainingWorkflow.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_workflows(self, project: Path, profile_id: str | None = None) -> list[TrainingWorkflow]:
        project = ensure_within(self.paths.projects_root, project); values = []
        for path in (project / "workflows").glob("*.json"):
            try:
                workflow = TrainingWorkflow.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if not profile_id or workflow.voice_profile_id == profile_id: values.append(workflow)
            except (OSError, ValueError, TypeError, json.JSONDecodeError): continue
        return sorted(values, key=lambda item: item.updated_at, reverse=True)

    def recover_workflows(self, project: Path) -> list[TrainingWorkflow]:
        workflows = self.list_workflows(project); active_profiles: set[str] = set()
        for workflow in workflows:
            if workflow.status == WorkflowStatus.RUNNING:
                workflow.status = WorkflowStatus.INTERRUPTED; workflow.message = "程序上次退出时流程尚未结束，可点击继续"; self.save_workflow(project, workflow)
            if workflow.status in {WorkflowStatus.RUNNING, WorkflowStatus.INTERRUPTED, WorkflowStatus.WAITING}: active_profiles.add(workflow.voice_profile_id)
        for profile in self.list_profiles(project):
            if profile.training_state and profile.id not in active_profiles: profile.training_state = ""; self.save_profile(project, profile)
        return workflows

    def save_draft(self, project: Path, draft: DatasetDraft) -> Path:
        project = ensure_within(self.paths.projects_root, project); draft.updated_at = utc_now(); folder = ensure_within(project, project / "drafts"); folder.mkdir(parents=True, exist_ok=True); path = folder / f"{draft.id}.json"; self._atomic_json(path, draft.to_dict()); return path

    def load_draft(self, project: Path, draft_id: str) -> DatasetDraft:
        validate_id(draft_id, legacy=True, field="draft_id")
        project = ensure_within(self.paths.projects_root, project); path = ensure_within(project / "drafts", project / "drafts" / f"{draft_id}.json"); return DatasetDraft.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def cleanup_preparation_runs(self, project: Path, profile_id: str) -> list[str]:
        project = ensure_within(self.paths.projects_root, project); protected: set[str] = set()
        for profile in self.list_profiles(project):
            if profile.id == profile_id and profile.current_preparation_id: protected.add(profile.current_preparation_id)
            for reference in profile.reference_assets:
                parts = Path(reference.path).parts
                if "runs" in parts:
                    index = parts.index("runs")
                    if index + 1 < len(parts): protected.add(parts[index + 1])
        for snapshot in self.load_project(project).get("dataset_snapshots", []):
            stored = Path(snapshot.get("path", "")); manifest = stored if stored.is_absolute() else project / stored
            if manifest.is_file():
                for segment in json.loads(manifest.read_text(encoding="utf-8")).get("segments", []):
                    parts = Path(segment.get("audio_path", "")).parts
                    if "runs" in parts:
                        index = parts.index("runs")
                        if index + 1 < len(parts): protected.add(parts[index + 1])
        removed: list[str] = []
        roots = [project / "processed" / profile_id / "runs", project / "datasets" / "working" / profile_id]
        for root in roots:
            if not root.is_dir(): continue
            for child in root.iterdir():
                target = ensure_within(root, child)
                if target.is_dir() and target.name not in protected:
                    shutil.rmtree(target); removed.append(str(target))
        return removed

    def save_dataset_snapshot(self, project: Path, dataset: DatasetManifest) -> Path:
        project = ensure_within(self.paths.projects_root, project)
        folder = ensure_within(project / "datasets", project / "datasets" / dataset.id)
        folder.mkdir(parents=True, exist_ok=True)
        payload = dataset.to_dict()
        payload["list_path"] = ""; payload["wav_dir"] = ""
        for segment in payload["segments"]: segment["audio_path"] = ""
        self._atomic_json(folder / "manifest.json", payload)
        manifest = self.load_project(project)
        snapshots = manifest.setdefault("dataset_snapshots", [])
        summary = {"id": dataset.id, "voice_profile_id": dataset.voice_profile_id, "path": (folder / "manifest.json").relative_to(project).as_posix(), "snapshot_sha256": dataset.snapshot_sha256, "approved_seconds": dataset.approved_seconds, "created_at": dataset.created_at}
        snapshots[:] = [item for item in snapshots if item.get("id") != dataset.id]
        snapshots.append(summary)
        self._atomic_json(project / "project.json", manifest)
        return folder / "manifest.json"

    def load_dataset_snapshot(self, project: Path, snapshot_id: str) -> DatasetManifest:
        validate_id(snapshot_id, legacy=True, field="snapshot_id")
        project = ensure_within(self.paths.projects_root, project)
        path = ensure_within(project / "datasets", project / "datasets" / snapshot_id / "manifest.json")
        value = json.loads(path.read_text(encoding="utf-8"))
        snapshot_root = path.parent.resolve(); audio_root = ensure_within(snapshot_root, snapshot_root / "audio"); legacy = int(value.get("schema_version", 1)) < 2
        migrated = legacy
        if legacy: audio_root.mkdir(parents=True, exist_ok=True)
        for segment in value.get("segments", []):
            relative = str(segment.get("audio_relative_path", "")).replace("\\", "/")
            candidate = (project / relative).resolve() if relative else Path(segment.get("audio_path", "")).resolve()
            if not candidate.is_file():
                fallback = audio_root / Path(segment.get("audio_path") or relative).name
                if legacy and fallback.is_file(): candidate = fallback.resolve(); migrated = True
                else: raise FileNotFoundError(f"旧快照音频无法迁移或文件缺失：{candidate}")
            if legacy:
                try:
                    ensure_within(audio_root, candidate)
                except ValueError:
                    digest = sha256_file(candidate)
                    suffix = candidate.suffix.lower() or ".wav"
                    copied = audio_root / f"{digest[:16]}{suffix}"
                    if not copied.exists(): shutil.copy2(candidate, copied)
                    candidate = copied.resolve(); migrated = True
            ensure_within(audio_root, candidate)
            actual_relative = candidate.relative_to(project.resolve()).as_posix()
            if not legacy and relative != actual_relative:
                raise ValueError(f"快照音频相对路径不一致：{candidate.name}")
            if relative != actual_relative: migrated = True
            segment["audio_relative_path"] = actual_relative; segment["audio_path"] = str(candidate)
            digest = sha256_file(candidate)
            if not segment.get("source_sha256") and legacy: segment["source_sha256"] = digest; migrated = True
            elif not segment.get("source_sha256"): raise ValueError(f"快照缺少音频哈希：{candidate.name}")
            elif segment["source_sha256"] != digest: raise ValueError(f"快照音频哈希不一致：{candidate.name}")
        list_path = snapshot_root / "dataset.list"; list_relative = list_path.relative_to(project.resolve()).as_posix()
        expected_lines = [f"{item['audio_relative_path']}|speaker|{item.get('language', 'zh')}|{item.get('text', '')}" for item in value.get("segments", [])]
        current_lines = list_path.read_text(encoding="utf-8").splitlines() if list_path.is_file() else []
        if current_lines != expected_lines:
            if not legacy: raise ValueError("冻结快照的标注清单内容已被修改")
            list_path.write_text("\n".join(expected_lines) + "\n", encoding="utf-8"); migrated = True
        actual_list_sha = sha256_file(list_path)
        if not legacy and value.get("list_sha256") != actual_list_sha:
            raise ValueError("冻结快照的标注清单哈希不一致")
        value.update({"schema_version": 2, "list_path": str(list_path), "wav_dir": str(audio_root), "list_relative_path": list_relative, "list_sha256": sha256_file(list_path)})
        dataset = DatasetManifest.from_dict(value); dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
        if not legacy and value.get("snapshot_sha256") != dataset.snapshot_sha256:
            raise ValueError("冻结快照清单哈希不一致")
        if migrated:
            self.save_dataset_snapshot(project, dataset)
        return dataset

    def save_generation_record(self, project: Path, record: GenerationRecord) -> None:
        project = ensure_within(self.paths.projects_root, project)
        manifest = self.load_project(project)
        if record.project_uid != manifest.get("project_uid"):
            raise ValueError("生成记录不属于当前项目")
        validate_id(record.id, field="generation_record_id")
        record.updated_at = utc_now()
        records = manifest.setdefault("generation_records", [])
        records[:] = [item for item in records if item.get("id") != record.id]
        records.append(record.to_dict())
        self._atomic_json(project / "project.json", manifest)

    def list_generation_records(self, project: Path, limit: int = 200) -> list[GenerationRecord]:
        values = self.load_project(project).get("generation_records", [])
        records = [GenerationRecord.from_dict(item) for item in values]
        return sorted(records, key=lambda item: item.updated_at, reverse=True)[:max(0, limit)]

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
