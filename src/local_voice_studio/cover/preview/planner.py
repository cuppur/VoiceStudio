from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
from typing import Mapping

from ..models import CoverAssetRole

# Preview uses the domain asset vocabulary directly.  This is deliberately an
# alias: there must be one role enum in the application, while ``parse`` keeps
# old IPC callers accepting ``ai-vocal`` and ``ai_vocal``.
TrackRole = CoverAssetRole


class PlaybackMode(str, Enum):
    """Playback policy, independent from the selected timeline row."""
    SOLO_TRACK = "solo_track"
    # Kept as a source-compatible alias for early Phase 4 callers.
    SELECTED_TRACK = "solo_track"
    MIX_PREVIEW = "mix_preview"
    FINAL_MIX = "final_mix"


@dataclass(frozen=True)
class PreviewTrack:
    role: TrackRole
    path: str = ""
    duration_ms: int = 0
    enabled: bool = True
    gain: float = 1.0
    muted: bool = False
    solo: bool = False
    asset_id: str = ""

    @property
    def available(self) -> bool:
        return bool(self.path)

    @property
    def mute(self) -> bool:
        """Short spelling used by the UI/reporting contract."""
        return self.muted


@dataclass(frozen=True)
class PreviewMixPlan:
    mode: PlaybackMode
    selected_role: TrackRole
    tracks: Mapping[TrackRole, PreviewTrack] = field(default_factory=dict)
    master_gain: float = 1.0

    @property
    def source(self) -> PreviewTrack | None:
        track = self.tracks.get(self.selected_role)
        return track if track and track in self.active_tracks else None

    @property
    def roles(self) -> set[TrackRole]:
        return {track.role for track in self.active_tracks}

    @property
    def active_tracks(self) -> tuple[PreviewTrack, ...]:
        available = [track for track in self.tracks.values()
                     if track.available and track.enabled and not track.muted and track.gain > 0.0]
        if self.mode is PlaybackMode.FINAL_MIX:
            return tuple(track for track in available if track.role is TrackRole.FINAL_MIX)
        if self.mode in (PlaybackMode.SOLO_TRACK, PlaybackMode.SELECTED_TRACK):
            selected = self.tracks.get(self.selected_role)
            return (selected,) if selected and selected.available and selected.enabled and not selected.muted and selected.gain > 0.0 else ()
        # A solo flag narrows a mix without changing the selected timeline row.
        solos = [track for track in available if track.solo]
        return tuple(solos or [track for track in available if track.role in {
            TrackRole.VOCAL, TrackRole.INSTRUMENTAL, TrackRole.AI_VOCAL
        }])


class PreviewMixPlanner:
    """Map semantic roles to channels; never infer meaning from array indexes."""

    def __init__(self, tracks: Mapping[TrackRole | str, PreviewTrack | str] | None = None):
        self._tracks: dict[TrackRole, PreviewTrack] = {}
        for key, value in (tracks or {}).items():
            role = TrackRole.parse(key)
            self._tracks[role] = value if isinstance(value, PreviewTrack) else PreviewTrack(role, str(value))

    @property
    def tracks(self) -> Mapping[TrackRole, PreviewTrack]:
        return dict(self._tracks)

    def set_track(self, track: PreviewTrack) -> None:
        self._tracks[track.role] = track

    def plan(self, selected_role: TrackRole | str, mode: PlaybackMode | str = PlaybackMode.SELECTED_TRACK) -> PreviewMixPlan:
        role = TrackRole.parse(selected_role)
        mode_text = str(mode).lower().replace("-", "_")
        if mode_text in {"mix", "preview", "mix_preview"}:
            mode_text = PlaybackMode.MIX_PREVIEW.value
        elif mode_text in {"solo", "selected", "selected_track"}:
            mode_text = PlaybackMode.SOLO_TRACK.value
        playback = mode if isinstance(mode, PlaybackMode) else PlaybackMode(mode_text)
        if playback is PlaybackMode.FINAL_MIX:
            role = TrackRole.FINAL_MIX
        return PreviewMixPlan(playback, role, _normalize_linear_gains(self.tracks), 1.0)

    def build(self, cover: object, *, mode: PlaybackMode | str = PlaybackMode.MIX_PREVIEW,
              selected_role: TrackRole | str = TrackRole.AI_VOCAL,
              settings: Mapping[str, object] | None = None) -> PreviewMixPlan:
        """Build a plan directly from a CoverProject-like asset registry.

        The planner only reads ``assets``/``root`` and therefore stays free of
        Qt, Worker IPC and filesystem mutation.  Gain values are linear at the
        preview boundary; the domain mixer remains dB-based.
        """
        if settings is not None and hasattr(settings, "canonical"):
            settings = settings.canonical()
        settings = settings or {}
        tracks: dict[TrackRole, PreviewTrack] = {}
        assets = getattr(cover, "assets", ())
        root = getattr(cover, "root", None)
        for asset in assets:
            try:
                role = TrackRole.parse(getattr(asset, "role"))
            except (TypeError, ValueError):
                continue
            relative = str(getattr(asset, "relative_path", ""))
            path = str(Path(root, relative)) if root and relative else ""
            gain_key = {
                TrackRole.AI_VOCAL: "ai_gain",
                TrackRole.INSTRUMENTAL: "instrumental_gain",
                TrackRole.VOCAL: "vocal_gain",
                TrackRole.ORIGINAL: "original_gain",
                TrackRole.FINAL_MIX: "final_gain",
            }[role]
            db_key = {
                "ai_gain": "ai_gain_db", "instrumental_gain": "instrumental_gain_db",
                "vocal_gain": "original_vocal_gain_db", "original_gain": "original_gain_db",
                "final_gain": "final_gain_db",
            }[gain_key]
            # A CoverMixSettings canonical mapping uses dB keys; preview
            # channels deliberately expose linear gain to QAudioOutput.
            if db_key in settings:
                raw_db = settings[db_key]
                gain = _db_to_linear(raw_db)
            else:
                gain = float(settings.get(gain_key, 1.0))
            tracks[role] = PreviewTrack(role, path=path, duration_ms=int(getattr(cover, "duration_ms", 0)),
                                        gain=gain, asset_id=str(getattr(asset, "id", "")))
        plan = PreviewMixPlanner(tracks).plan(selected_role, mode)
        # Normalise only at this preview boundary.  The largest effective
        # channel gain (including master gain) maps to 1.0, preserving all
        # inter-track dB differences while preventing QAudioOutput clamping
        # from silently flattening a positive-gain mix.
        if "master_gain_db" in settings:
            raw_master = settings["master_gain_db"]
            master_db = _as_db(raw_master)
        else:
            master = float(settings.get("master_gain", 1.0))
            master_db = 20.0 * math.log10(master) if master > 0 else float("-inf")
        finite_db = [
            20.0 * math.log10(track.gain)
            for track in tracks.values()
            if track.available and track.enabled and not track.muted and track.gain > 0.0
        ]
        peak_db = max([master_db + db for db in finite_db] or [master_db])
        normalizer_db = max(0.0, peak_db)
        normalized = {
            role: PreviewTrack(track.role, track.path, track.duration_ms, track.enabled,
                               _linear_from_db((20.0 * math.log10(track.gain) + master_db) - normalizer_db)
                               if track.gain > 0.0 and math.isfinite(master_db) else 0.0,
                               track.muted, track.solo, track.asset_id)
            for role, track in tracks.items()
        }
        return PreviewMixPlan(plan.mode, plan.selected_role, normalized, 1.0)


def _as_db(value: object) -> float:
    if isinstance(value, str) and value.strip().lower() in {"-inf", "-infinity"}:
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的预览增益: {value!r}") from exc


def _db_to_linear(value: object) -> float:
    db = _as_db(value)
    return _linear_from_db(db)


def _linear_from_db(db: float) -> float:
    return 0.0 if not math.isfinite(db) else 10.0 ** (db / 20.0)


def _normalize_linear_gains(tracks: Mapping[TrackRole, PreviewTrack]) -> dict[TrackRole, PreviewTrack]:
    """Keep preview-only channel gains bounded without changing domain dB."""
    peak = max((track.gain for track in tracks.values()
                if track.available and track.enabled and not track.muted and track.gain > 0.0), default=1.0)
    scale = 1.0 / peak if peak > 1.0 else 1.0
    return {
        role: PreviewTrack(track.role, track.path, track.duration_ms, track.enabled,
                           track.gain * scale, track.muted, track.solo, track.asset_id)
        for role, track in tracks.items()
    }
