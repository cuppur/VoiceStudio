from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
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


ASSET_ROLES = {"original", "vocal", "instrumental", "ai_vocal"}


@dataclass
class CoverAsset:
    """A project-owned audio artifact and its provenance."""

    id: str
    role: str
    relative_path: str
    sha256: str
    content_origin: str
    producer: str
    producer_version: str = ""
    model_id: str = ""
    model_sha256: str = ""
    source_asset_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        try:
            validate_id(self.id, legacy=True, field="asset_id")
        except ValueError as exc:
            raise CoverProjectError(str(exc)) from exc
        if self.role not in ASSET_ROLES:
            raise CoverProjectError(f"不支持的资产角色: {self.role}")
        self.content_origin = content_origin(self.content_origin)
        path_text = str(self.relative_path).replace("\\", "/")
        if (not path_text or path_text.startswith("/") or Path(path_text).is_absolute()
                or PureWindowsPath(path_text).is_absolute() or ".." in Path(path_text).parts):
            raise CoverProjectError("asset relative_path 必须是项目内相对路径")
        self.relative_path = path_text
        # Hashes are required for persisted assets, but migration can retain a
        # missing hash when the old file is no longer available.
        if self.sha256:
            validate_sha256(self.sha256, field="asset_sha256")
        if self.model_sha256:
            validate_sha256(self.model_sha256, field="model_sha256")
        for source_id in self.source_asset_ids:
            validate_id(source_id, legacy=True, field="source_asset_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "role": self.role, "relative_path": self.relative_path,
            "sha256": self.sha256, "content_origin": self.content_origin,
            "producer": self.producer, "producer_version": self.producer_version,
            "model_id": self.model_id, "model_sha256": self.model_sha256,
            "source_asset_ids": list(self.source_asset_ids), "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CoverAsset":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


@dataclass
class CoverProject:
    """A cover session whose files are owned by ``project/covers/<id>``."""

    project_root: str
    id: str = field(default_factory=lambda: uuid4().hex)
    source_path: str = ""
    original_source_name: str = ""
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
    assets: list[CoverAsset] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    SCHEMA_VERSION = 2

    def __post_init__(self) -> None:
        self.project_root = str(Path(self.project_root).resolve())
        try:
            validate_id(self.id, legacy=True, field="cover_id")
        except ValueError as exc:
            raise CoverProjectError(str(exc)) from exc
        self.content_origin = content_origin(self.content_origin)
        if self.rights_attestation_version < 1:
            raise CoverProjectError("rights_attestation_version 必须为正整数")
        normalized_assets: list[CoverAsset] = []
        for asset in self.assets:
            normalized_assets.append(asset if isinstance(asset, CoverAsset) else CoverAsset.from_dict(asset))
        self.assets = normalized_assets
        ids = [asset.id for asset in self.assets]
        if len(ids) != len(set(ids)):
            raise CoverProjectError("assets 不允许重复 asset id")

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

    @property
    def ai_vocal_path(self) -> str:
        """Compatibility view of the newest registered AI vocal asset."""
        asset = self.get_asset(role="ai_vocal")
        return asset.relative_path if asset else ""

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
        version = value.get("schema_version", 1)
        if version not in (1, cls.SCHEMA_VERSION):
            raise CoverProjectError("不支持的翻唱项目版本")
        value.pop("schema_version", None)
        value.pop("manifest_path", None)
        if value.get("source_audio") and not value.get("source_relative_path"):
            value["source_relative_path"] = value["source_audio"]
        if value.get("source_audio_sha256") and not value.get("source_sha256"):
            value["source_sha256"] = value["source_audio_sha256"]
        if version == 1:
            value["original_source_name"] = value.get("original_source_name") or (
                Path(value.get("source_path", "")).name or Path(value.get("source_relative_path", "")).name
            )
            migrated: list[CoverAsset] = []
            if value.get("source_relative_path"):
                migrated.append(CoverAsset(
                    id="original", role="original", relative_path=value["source_relative_path"],
                    sha256=value.get("source_sha256", ""), content_origin="original", producer="imported",
                ))
            for role, key in (("vocal", "vocal_path"), ("instrumental", "instrumental_path")):
                if value.get(key):
                    path = value[key]
                    digest = ""
                    candidate = root / path
                    if candidate.is_file():
                        digest = _sha256(candidate)
                    migrated.append(CoverAsset(
                        id=role, role=role, relative_path=path, sha256=digest,
                        content_origin="separated", producer="migrated",
                    ))
            value["assets"] = migrated
        value["project_root"] = str(Path(project_root).resolve())
        project = cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})
        project._validate_recorded_paths()
        if version == 1:
            # Persist the lossless schema-v2 representation immediately so a
            # migrated project no longer carries the legacy absolute source
            # path on disk.
            project.save()
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
            "id": self.id, "original_source_name": self.original_source_name,
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
            "assets": [asset.to_dict() for asset in self.assets],
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
        self.original_source_name = source.name
        self.source_relative_path = destination.relative_to(self.root).as_posix()
        self.source_sha256 = _sha256(destination)
        self._upsert_asset(CoverAsset(
            id="original", role="original", relative_path=self.source_relative_path,
            sha256=self.source_sha256, content_origin="original", producer="imported",
        ))
        self.title = source.stem
        self.save()
        return destination

    def set_stem(self, name: str, path: Path) -> None:
        relative = self._relative_owned(path)
        if name == "vocal": self.vocal_path = relative; origin = "separated"
        elif name == "instrumental": self.instrumental_path = relative; origin = "separated"
        else: raise CoverProjectError(f"不支持的音轨: {name}")
        self._upsert_asset(CoverAsset(
            id=name, role=name, relative_path=relative, sha256=_sha256(Path(path)),
            content_origin=origin, producer="separation",
        ))
        self.save()

    def _upsert_asset(self, asset: CoverAsset) -> None:
        self.assets = [item for item in self.assets if item.id != asset.id]
        self.assets.append(asset)

    def add_asset(self, asset: CoverAsset, *, save: bool = True) -> CoverAsset:
        """Append a new asset version, rejecting accidental ID reuse.

        Multiple assets may have the same role (for example successive AI
        vocal conversions); :meth:`get_asset` resolves a role to the newest
        registration while ID lookup remains exact.
        """
        if not isinstance(asset, CoverAsset):
            asset = CoverAsset.from_dict(asset)
        if any(existing.id == asset.id for existing in self.assets):
            raise CoverProjectError(f"资产 ID 已存在: {asset.id}")
        candidate = ensure_within(self.root, self.root / asset.relative_path)
        if asset.sha256 and candidate.is_file() and _sha256(candidate) != asset.sha256:
            raise CoverProjectError(f"资产 Hash 不匹配: {asset.id}")
        self.assets.append(asset)
        if save:
            self.save()
        return asset

    def get_asset(self, identifier: str | None = None, *, role: str | None = None) -> CoverAsset | None:
        """Return an asset by ID, or the newest asset with the given role."""
        identifier = role if role is not None else identifier
        if identifier is None:
            return None
        exact = next((asset for asset in self.assets if asset.id == identifier), None)
        if exact is not None:
            return exact
        for asset in reversed(self.assets):
            if asset.role == identifier:
                return asset
        return None

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
        for asset in self.assets:
            candidate = ensure_within(self.root, self.root / asset.relative_path)
            if asset.sha256:
                validate_sha256(asset.sha256, field="asset_sha256")
                # Loading a project must remain possible after on-disk damage;
                # cache and conversion callers perform the actual digest check
                # before use and can then repair or report the affected asset.
            if asset.model_sha256:
                validate_sha256(asset.model_sha256, field="model_sha256")
        if self.source_sha256:
            validate_sha256(self.source_sha256, field="source_sha256")
        if self.separator_model_sha256:
            validate_sha256(self.separator_model_sha256, field="separator_model_sha256")
