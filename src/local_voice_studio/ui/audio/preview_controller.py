from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ...cover.preview import PlaybackMode, PreviewMixPlan, PreviewMixPlanner, PreviewTrack, TrackRole


@dataclass
class PlayerChannel:
    """A small adapter around QMediaPlayer (or a test double)."""
    player: Any
    output: Any = None

    def set_source(self, path: str) -> None:
        if hasattr(self.player, "setSource"):
            try:
                from PySide6.QtCore import QUrl
                self.player.setSource(QUrl.fromLocalFile(path))
            except ImportError:
                self.player.setSource(path)
        elif hasattr(self.player, "set_source"):
            self.player.set_source(path)

    def set_position(self, position_ms: int) -> None:
        (self.player.setPosition if hasattr(self.player, "setPosition") else self.player.set_position)(int(position_ms))

    def position(self) -> int:
        value = self.player.position() if callable(getattr(self.player, "position", None)) else getattr(self.player, "position", 0)
        return int(value or 0)

    def play(self) -> None: self.player.play()
    def pause(self) -> None: self.player.pause()
    def stop(self) -> None: self.player.stop()

    def set_gain(self, gain: float) -> None:
        if self.output is not None and hasattr(self.output, "setVolume"):
            self.output.setVolume(max(0.0, min(1.0, float(gain))))


class PreviewAudioController:
    """Coordinates semantic preview channels against one master timeline clock."""

    def __init__(self, channels: Mapping[TrackRole | str, PlayerChannel] | None = None,
                 drift_tolerance_ms: int = 80,
                 player_factory: Callable[[TrackRole], Any] | None = None):
        self.channels: dict[TrackRole, PlayerChannel] = {}
        for key, channel in (channels or {}).items():
            self.channels[TrackRole.parse(key)] = channel
        if player_factory is not None:
            for role in TrackRole:
                if role not in self.channels:
                    created = player_factory(role)
                    self.channels[role] = created if isinstance(created, PlayerChannel) else PlayerChannel(created)
        self.drift_tolerance_ms = max(0, int(drift_tolerance_ms))
        self.plan: PreviewMixPlan | None = None
        self.master_position_ms = 0
        self.master_gain = 1.0
        self.playing = False
        self.cancelled = False

    @classmethod
    def create_qt(cls, parent: Any = None, *, drift_tolerance_ms: int = 80) -> "PreviewAudioController":
        """Create the semantic player pool; CoverPage never owns topology."""
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

        def factory(_role: TrackRole) -> PlayerChannel:
            output = QAudioOutput(parent); output.setVolume(.75)
            player = QMediaPlayer(parent); player.setAudioOutput(output)
            return PlayerChannel(player, output)

        return cls(player_factory=factory, drift_tolerance_ms=drift_tolerance_ms)

    def apply_plan(self, plan: PreviewMixPlan) -> None:
        self.stop()
        self.plan = plan
        self.master_gain = float(plan.master_gain)
        self.cancelled = False
        for role, track in plan.tracks.items():
            channel = self.channels.get(role)
            if channel and track.available:
                channel.set_source(track.path)
                channel.set_gain(0.0 if track.muted or not track.enabled else track.gain * self.master_gain)

    load = apply_plan

    def select(self, role: TrackRole | str) -> None:
        if self.plan is None: raise RuntimeError("preview plan has not been loaded")
        self.plan = PreviewMixPlan(PlaybackMode.SOLO_TRACK, TrackRole.parse(role), self.plan.tracks)

    def seek(self, position_ms: int) -> None:
        self.master_position_ms = max(0, int(position_ms))
        for channel in self.channels.values(): channel.set_position(self.master_position_ms)

    def play(self) -> None:
        if self.plan is None or not self.plan.active_tracks: return
        self.cancelled = False
        self.playing = True
        active = {track.role for track in self.plan.active_tracks}
        for role, channel in self.channels.items():
            if role in active: channel.play()
            else: channel.pause()

    def pause(self) -> None:
        self.playing = False
        for channel in self.channels.values(): channel.pause()

    def stop(self) -> None:
        self.playing = False
        for channel in self.channels.values(): channel.stop()

    def cancel(self) -> None:
        self.cancelled = True
        self.stop()

    def resync(self) -> int:
        """Seek slave channels back to the master clock when drift is large."""
        if self.plan is None or not self.plan.active_tracks: return 0
        master_role = self.master_role
        channel = self.channels.get(master_role)
        if channel is None: return 0
        drift = channel.position() - self.master_position_ms
        if abs(drift) > self.drift_tolerance_ms:
            channel.set_position(self.master_position_ms)
        for track in self.plan.active_tracks:
            slave = self.channels.get(track.role)
            if slave and track.role is not master_role and abs(slave.position() - self.master_position_ms) > self.drift_tolerance_ms:
                slave.set_position(self.master_position_ms)
        return drift

    @property
    def master_role(self) -> TrackRole | None:
        if self.plan is None:
            return None
        if self.plan.mode is PlaybackMode.FINAL_MIX:
            return TrackRole.FINAL_MIX
        if self.plan.mode is PlaybackMode.SOLO_TRACK:
            return self.plan.selected_role
        if TrackRole.AI_VOCAL in self.channels and self.plan.tracks.get(TrackRole.AI_VOCAL, PreviewTrack(TrackRole.AI_VOCAL)).available:
            return TrackRole.AI_VOCAL
        return TrackRole.INSTRUMENTAL

    def set_gain(self, role: TrackRole | str, gain: float) -> None:
        channel = self.channels.get(TrackRole.parse(role))
        if channel:
            channel.set_gain(gain)

    def set_master_gain(self, gain: float) -> None:
        self.master_gain = max(0.0, float(gain))
        if self.plan is None:
            return
        for track in self.plan.tracks.values():
            channel = self.channels.get(track.role)
            if channel:
                channel.set_gain(0.0 if track.muted or not track.enabled else track.gain * self.master_gain)
