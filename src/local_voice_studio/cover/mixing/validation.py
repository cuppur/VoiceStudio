"""The single input-validation boundary for final cover rendering."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ...audio import probe_audio, sha256_file
from ...paths import ensure_within
from ..cancellation import as_cancellation_token
from ..errors import AssetValidationError, MixAlignmentError, RightsRequiredError
from ..models import ContentOrigin, CoverAssetRole
from ..project import CoverAsset, CoverProject, RIGHTS_ATTESTATION_TEXT_HASH
from .models import CoverMixSettings, MixInput


@dataclass(frozen=True)
class AudioInfo:
    """The probe facts needed by the mixer, copied out of the probe object."""

    duration_seconds: float
    sample_rate: int = 0
    channels: int = 0

    @classmethod
    def from_probe(cls, value: Any) -> "AudioInfo":
        try:
            duration = float(getattr(value, "duration_seconds"))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AssetValidationError("输入音频缺少有效时长") from exc
        return cls(
            duration_seconds=duration,
            sample_rate=int(getattr(value, "sample_rate", 0) or 0),
            channels=int(getattr(value, "channels", 0) or 0),
        )


@dataclass(frozen=True)
class ResolvedAudioAsset:
    """An asset after ownership, origin, hash and probe validation."""

    role: CoverAssetRole
    asset_id: str
    path: Path
    sha256: str
    duration_seconds: float
    sample_rate: int
    channels: int
    gain_db: float

    @property
    def audio_info(self) -> AudioInfo:
        return AudioInfo(self.duration_seconds, self.sample_rate, self.channels)

    def as_mix_input(self) -> MixInput:
        return MixInput(self.role, self.asset_id, self.path, self.sha256, self.gain_db)


@dataclass(frozen=True)
class ResolvedMixInputs:
    """Validated mixer inputs and the one probe result for every input."""

    inputs: tuple[MixInput, ...]
    duration_seconds: float
    probes: Mapping[CoverAssetRole, AudioInfo]
    assets: Mapping[CoverAssetRole, CoverAsset]

    @property
    def resolved_assets(self) -> tuple[ResolvedAudioAsset, ...]:
        return tuple(
            ResolvedAudioAsset(
                role=item.role,
                asset_id=item.asset_id,
                path=item.path,
                sha256=item.sha256,
                duration_seconds=self.probes[item.role].duration_seconds,
                sample_rate=self.probes[item.role].sample_rate,
                channels=self.probes[item.role].channels,
                gain_db=item.gain_db,
            )
            for item in self.inputs
        )


@dataclass(frozen=True)
class MixAlignmentReport:
    reference_duration_seconds: float
    max_delta_ms: float
    warning: bool = False


class MixAlignmentValidator:
    """Validate duration alignment without mixing or probing audio."""

    @staticmethod
    def validate(
        inputs: ResolvedMixInputs | Sequence[ResolvedAudioAsset],
        tolerance_ms: int = 250,
        hard_limit_ms: int = 1000,
        probes: Mapping[CoverAssetRole, AudioInfo] | None = None,
    ) -> MixAlignmentReport:
        if tolerance_ms < 0 or hard_limit_ms < 0 or tolerance_ms > hard_limit_ms:
            raise ValueError("音频对齐容差无效")
        if isinstance(inputs, ResolvedMixInputs):
            resolved = inputs.resolved_assets
        else:
            resolved = tuple(inputs)
            if probes is not None:
                resolved = tuple(
                    item if isinstance(item, ResolvedAudioAsset) else ResolvedAudioAsset(
                        role=_role(getattr(item, "role")),
                        asset_id=str(getattr(item, "asset_id", "")),
                        path=Path(getattr(item, "path", "")),
                        sha256=str(getattr(item, "sha256", "")),
                        duration_seconds=probes[_role(getattr(item, "role"))].duration_seconds,
                        sample_rate=probes[_role(getattr(item, "role"))].sample_rate,
                        channels=probes[_role(getattr(item, "role"))].channels,
                        gain_db=float(getattr(item, "gain_db", 0.0)),
                    )
                    for item in resolved
                )
        if not resolved:
            raise AssetValidationError("没有可用于混音的音频输入")
        durations = [float(item.duration_seconds) for item in resolved]
        reference = max(durations)
        max_delta_ms = max((reference - value) * 1000.0 for value in durations)
        if max_delta_ms > hard_limit_ms:
            raise MixAlignmentError(
                f"音频时长差异超过 {hard_limit_ms} ms（最大 {max_delta_ms:.1f} ms）"
            )
        return MixAlignmentReport(reference, max_delta_ms, max_delta_ms > tolerance_ms)


def _role(value: CoverAssetRole | str) -> CoverAssetRole:
    if isinstance(value, CoverAssetRole):
        return value
    if isinstance(value, Enum):
        value = value.value
    return CoverAssetRole(str(value).strip().lower().replace("-", "_"))


class MixValidator:
    """Resolve project assets exactly once before a backend is called."""

    def __init__(self, project: CoverProject):
        self.project = project

    def require_rights(self, confirmed: bool | None = None) -> None:
        if (
            confirmed is False
            or not self.project.rights_confirmed
            or self.project.rights_attestation_text_hash != RIGHTS_ATTESTATION_TEXT_HASH
        ):
            raise RightsRequiredError("混音前必须确认歌曲处理权利")

    def asset(
        self,
        role: CoverAssetRole | str,
        *,
        origin: ContentOrigin | str | None = None,
        cancel: Any = None,
    ) -> CoverAsset:
        semantic_role = _role(role)
        asset = self.project.get_asset(role=semantic_role)
        if asset is None:
            raise AssetValidationError(f"缺少 {semantic_role.value} 资产")
        if origin is not None:
            expected_origin = origin.value if isinstance(origin, Enum) else str(origin)
            if asset.content_origin != expected_origin:
                raise AssetValidationError(f"{semantic_role.value} 资产来源无效")
        try:
            path = ensure_within(self.project.root, self.project.root / asset.relative_path)
        except (OSError, ValueError) as exc:
            raise AssetValidationError(f"{semantic_role.value} 资产路径无效") from exc
        if not path.is_file() or not asset.sha256:
            raise AssetValidationError(f"{semantic_role.value} 资产缺失或 Hash 不匹配")
        try:
            digest = sha256_file(path, cancel=cancel)
        except AssetValidationError:
            raise
        except Exception as exc:
            if cancel is not None and as_cancellation_token(cancel).is_cancelled():
                raise InterruptedError("混音已取消") from exc
            raise AssetValidationError(f"{semantic_role.value} 资产无法读取") from exc
        if digest != asset.sha256:
            raise AssetValidationError(f"{semantic_role.value} 资产缺失或 Hash 不匹配")
        return asset

    @staticmethod
    def _probe(probe: Callable[..., Any], path: Path, token: Any) -> AudioInfo:
        try:
            value = probe(path, cancel=token)
        except TypeError as exc:
            # Small fakes in downstream integrations often only accept path.
            if "cancel" not in str(exc):
                raise
            value = probe(path)
        except Exception as exc:
            if token.is_cancelled():
                raise InterruptedError("混音已取消") from exc
            raise
        info = AudioInfo.from_probe(value)
        if info.duration_seconds <= 0:
            raise AssetValidationError(f"输入音频时长异常: {path.name}")
        return info

    def resolve_inputs(
        self,
        settings: CoverMixSettings,
        *,
        probe: Callable[..., Any] | None = None,
        cancel: Any = None,
    ) -> ResolvedMixInputs:
        """Validate, hash and probe every selected input exactly once."""
        self.require_rights()
        probe_fn = probe or probe_audio
        token = as_cancellation_token(cancel)
        requested: tuple[tuple[CoverAssetRole, ContentOrigin, float], ...] = (
            (CoverAssetRole.INSTRUMENTAL, ContentOrigin.SEPARATED, settings.instrumental_gain_db),
            (CoverAssetRole.AI_VOCAL, ContentOrigin.AI_GENERATED, settings.ai_gain_db),
            (CoverAssetRole.VOCAL, ContentOrigin.SEPARATED, settings.original_vocal_gain_db),
        )
        inputs: list[MixInput] = []
        probes: dict[CoverAssetRole, AudioInfo] = {}
        assets: dict[CoverAssetRole, CoverAsset] = {}
        for role, origin, gain in requested:
            if gain == float("-inf"):
                continue
            if token.is_cancelled():
                raise InterruptedError("混音已取消")
            asset = self.asset(role, origin=origin, cancel=token)
            path = ensure_within(self.project.root, self.project.root / asset.relative_path)
            info = self._probe(probe_fn, path, token)
            assets[role] = asset
            probes[role] = info
            inputs.append(MixInput(role, asset.id, path, asset.sha256, float(gain)))
        if not inputs:
            raise AssetValidationError("没有可用于混音的音频输入")
        duration = max(info.duration_seconds for info in probes.values())
        return ResolvedMixInputs(tuple(inputs), duration, dict(probes), dict(assets))
