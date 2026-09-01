from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..paths import ensure_within, validate_id, validate_sha256


class CoverProjectError(ValueError):
    """Invalid or unsafe cover-project data."""


def content_origin(value: str) -> str:
    """Normalize the declared origin of cover content.

    Keep this contract deliberately small so separated stems cannot be
    mistaken for a future AI-generated export.
    """
    normalized = str(value).strip().lower().replace(" ", "_")
    allowed = {"original", "separated", "ai_generated"}
    if normalized not in allowed:
        raise CoverProjectError(f"不支持的内容来源: {value}")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class CoverProject:
    """A cover session whose files are owned by ``project/covers/<id>``."""

    project_root: str
    id: str = field(default_factory=lambda: uuid4().hex)
    source_path: str = ""
    source_relative_path: str = ""
    source_sha256: str = ""
    title: str = "未命名翻唱"
    duration_ms: int = 0
    vocal_path: str = ""
    instrumental_path: str = ""
    separator: str = ""
    separator_model_sha256: str = ""
    separation_cache_key: str = ""
    separation_status: str = "not_started"
    lyrics_path: str = ""
    waveform_path: str = ""
    waveform_paths: dict[str, str] = field(default_factory=dict)
    rights_attestation_version: int = 1
    rights_confirmed: bool = False
    rights_confirmed_at: str = ""
    content_origin: str = "original"
    output_hashes: dict[str, str] = field(default_factory=dict)
    output_paths: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        self.project_root = str(Path(self.project_root).resolve())
        try:
            validate_id(self.id, legacy=True, field="cover_id")
        except ValueError as exc:
            raise CoverProjectError(str(exc)) from exc
        self.content_origin = content_origin(self.content_origin)
        if self.rights_attestation_version < 1:
            raise CoverProjectError("rights_attestation_version 必须为正整数")

    @property
    def root(self) -> Path:
        return ensure_within(Path(self.project_root) / "covers", Path(self.project_root) / "covers" / self.id)

    @property
    def manifest_path(self) -> Path:
        return ensure_within(self.root, self.root / "manifest.json")

    @property
    def source_audio(self) -> str:
        """Compatibility alias for the project-owned source relative path."""
        return self.source_relative_path

    @property
    def source_audio_sha256(self) -> str:
        return self.source_sha256

    @classmethod
    def create(cls, project_root: Path, *, title: str = "未命名翻唱", cover_id: str | None = None, **kwargs: Any) -> "CoverProject":
        if "name" in kwargs and title == "未命名翻唱":
            title = kwargs.pop("name")
        project = cls(str(project_root), id=cover_id or uuid4().hex, title=title, **kwargs)
        project.root.mkdir(parents=True, exist_ok=True)
        for child in ("source", "stems", "lyrics", "waveform", "outputs"):
            (project.root / child).mkdir(exist_ok=True)
        project.save()
        return project

    @classmethod
    def load(cls, project_root: Path, cover_id: str) -> "CoverProject":
        # A project created before CoverProject simply has no cover records.
        validate_id(cover_id, legacy=True, field="cover_id")
        root = ensure_within(Path(project_root).resolve() / "covers", Path(project_root).resolve() / "covers" / cover_id)
        path = ensure_within(root, root / "manifest.json")
        if not path.is_file():
            raise FileNotFoundError(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != cls.SCHEMA_VERSION:
            raise CoverProjectError("不支持的翻唱项目版本")
        value.pop("schema_version", None)
        value.pop("manifest_path", None)
        if value.get("source_audio") and not value.get("source_relative_path"):
            value["source_relative_path"] = value["source_audio"]
        if value.get("source_audio_sha256") and not value.get("source_sha256"):
            value["source_sha256"] = value["source_audio_sha256"]
        value["project_root"] = str(Path(project_root).resolve())
        project = cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})
        project._validate_recorded_paths()
        return project

    @classmethod
    def list(cls, project_root: Path) -> list["CoverProject"]:
        covers = Path(project_root).resolve() / "covers"
        if not covers.is_dir():
            return []
        result = []
        for path in sorted(covers.iterdir()):
            if path.is_dir() and (path / "manifest.json").is_file():
                try:
                    result.append(cls.load(project_root, path.name))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "id": self.id, "source_path": self.source_path,
            "source_relative_path": self.source_relative_path,
            "source_sha256": self.source_sha256,
            "title": self.title, "duration_ms": self.duration_ms,
            "vocal_path": self.vocal_path, "instrumental_path": self.instrumental_path,
            "separator": self.separator, "separator_model_sha256": self.separator_model_sha256,
            "separation_cache_key": self.separation_cache_key,
            "separation_status": self.separation_status, "lyrics_path": self.lyrics_path,
            "waveform_path": self.waveform_path,
            "waveform_paths": dict(self.waveform_paths),
            "source_audio": self.source_relative_path,
            "source_audio_sha256": self.source_sha256,
            "rights_attestation_version": self.rights_attestation_version,
            "rights_confirmed": self.rights_confirmed,
            "rights_confirmed_at": self.rights_confirmed_at,
            "content_origin": self.content_origin,
            "output_hashes": dict(self.output_hashes),
            "output_paths": dict(self.output_paths),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def save(self) -> Path:
        self._validate_recorded_paths()
        self.root.mkdir(parents=True, exist_ok=True)
        self.updated_at = _now()
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.manifest_path)
        return self.manifest_path

    def copy_source(self, source: Path) -> Path:
        if self.source_relative_path:
            raise CoverProjectError("CoverProject 的源歌曲副本不可覆盖，请新建歌曲工程")
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = ensure_within(self.root, self.root / "source" / source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self.source_path = str(source)
        self.source_relative_path = destination.relative_to(self.root).as_posix()
        self.source_sha256 = _sha256(destination)
        self.title = source.stem
        self.save()
        return destination

    def set_stem(self, name: str, path: Path) -> None:
        relative = self._relative_owned(path)
        if name == "vocal": self.vocal_path = relative
        elif name == "instrumental": self.instrumental_path = relative
        else: raise CoverProjectError(f"不支持的音轨: {name}")
        self.save()

    def set_lyrics(self, path: Path) -> None:
        self.lyrics_path = self._relative_owned(path)
        self.save()

    def set_waveform(self, path: Path, track: str = "mix") -> None:
        relative = self._relative_owned(path)
        self.waveform_path = relative
        self.waveform_paths[str(track)] = relative
        self.save()

    def attest_rights(self, confirmed: bool = True, *, version: int | None = None, confirmed_at: str | None = None) -> None:
        if version is not None:
            if int(version) < 1:
                raise CoverProjectError("rights_attestation_version 必须为正整数")
            self.rights_attestation_version = int(version)
        self.rights_confirmed = bool(confirmed)
        self.rights_confirmed_at = (confirmed_at or _now()) if self.rights_confirmed else ""
        self.save()

    def register_output(self, path: Path, name: str | None = None) -> str:
        relative = self._relative_owned(path)
        absolute = self.root / relative
        digest = _sha256(absolute)
        key = str(name or relative)
        self.output_hashes[key] = digest
        self.output_paths[key] = relative
        self.save()
        return digest

    def verify_outputs(self) -> bool:
        try:
            required = {"vocal", "instrumental"}
            if set(self.output_paths) != required or set(self.output_hashes) != required:
                return False
            return all(_sha256(self.root / self.output_paths[key]) == self.output_hashes[key] for key in required)
        except (OSError, ValueError):
            return False

    def _relative_owned(self, path: Path) -> str:
        candidate = ensure_within(self.root, Path(path))
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return candidate.relative_to(self.root).as_posix()

    def _validate_recorded_paths(self) -> None:
        for value in [self.source_relative_path, self.vocal_path, self.instrumental_path, self.lyrics_path, self.waveform_path]:
            if value:
                ensure_within(self.root, self.root / value)
        for value in self.waveform_paths.values():
            ensure_within(self.root, self.root / value)
        for digest in self.output_hashes.values():
            validate_sha256(digest, allow_empty=False, field="output_sha256")
        for value in self.output_paths.values():
            ensure_within(self.root, self.root / value)
        if self.source_sha256:
            validate_sha256(self.source_sha256, field="source_sha256")
        if self.separator_model_sha256:
            validate_sha256(self.separator_model_sha256, field="separator_model_sha256")
