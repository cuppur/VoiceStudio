"""Qt playback adapter for the existing preview planner and waveform decoder."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QRunnable, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from ....cover.preview.planner import PreviewMixPlanner, PlaybackMode, TrackRole
from ....cover.project import CoverProject
from ....paths import ensure_within
from ...cover_session import SongSession
from .base import WebService
from .cover import CoverService


class _WaveTask(QRunnable):
    def __init__(self, cover, paths, key, done, cancel):
        super().__init__()
        self.cover, self.paths, self.key, self.done = cover, paths, key, done
        self.cancel = cancel

    def run(self):
        tracks = []
        for asset in self.cover.assets:
            if self.cancel(): return
            if asset.role not in {'original', 'vocal', 'instrumental', 'ai_vocal', 'final_mix'}:
                continue
            try:
                path = ensure_within(self.cover.root, self.cover.root / asset.relative_path)
                session = SongSession.load(path, paths=self.paths, cache_dir=self.cover.root / 'waveform', peak_count=1200, cancel=self.cancel)
                tracks.append({'role': str(asset.role), 'path': str(path), 'peaks': session.peaks,
                               'metadata': session.to_dict()['metadata']})
            except Exception as exc:
                tracks.append({'role': str(asset.role), 'peaks': [], 'error': str(exc)})
        if not self.cancel():
            self.done({'project': str(self.cover.project_root), 'cover_id': self.cover.id, 'tracks': tracks, '_key': self.key})


class MediaService(WebService):
    analyzed = Signal(dict)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.players = {}
        self.outputs = {}
        self.cover_id = ''
        self.plan = None
        self.playing = False
        self.position = 0
        self.volume = .8
        self._analysis_keys = {}
        self._latest = {}
        self.analyzed.connect(self._analyzed)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)

    def analyze(self, cover_id):
        cover = CoverProject.load(self.project, cover_id)
        key = (str(self.project), cover_id, tuple((a.role, a.sha256) for a in cover.assets))
        self._latest[(str(self.project), cover_id)] = key
        if key not in self._analysis_keys:
            self._analysis_keys[key] = None
            QThreadPool.globalInstance().start(_WaveTask(cover, self.paths, key, self.analyzed.emit, lambda: self._closing))
        elif self._analysis_keys[key] is not None:
            self.notify('cover.media', self._analysis_keys[key])
        return {'ok': True}

    def _analyzed(self, result):
        key = result.pop('_key')
        self._analysis_keys[key] = result
        if not self._closing and result['project'] == str(self.project) and self._latest.get(key[:2]) == key:
            self.notify('cover.media', result)

    def configure(self, data):
        cover = CoverProject.load(self.project, str(data.get('cover_id', '')))
        if cover.id != self.cover_id:
            self.stop()
            self.position = 0
            self.cover_id = cover.id
        planner = PreviewMixPlanner()
        mode = str(data.get('mode', 'solo_track'))
        settings = CoverService.mix_settings(data.get('mix') or {}).canonical() if mode == 'mix_preview' else {}
        base = planner.build(cover, settings=settings)
        muted, solo = set(data.get('muted') or []), set(data.get('solo') or [])
        for role, track in base.tracks.items():
            path = ensure_within(cover.root, Path(track.path))
            planner.set_track(replace(track, path=str(path) if path.is_file() else '',
                                      muted=role.value in muted, solo=role.value in solo))
        self.plan = planner.plan(str(data.get('role', 'original')), mode)
        active = {track.role.value: track for track in self.plan.active_tracks}
        for role, track in self.plan.tracks.items():
            key = role.value
            if key not in self.players:
                output = QAudioOutput(self)
                player = QMediaPlayer(self)
                player.setAudioOutput(output)
                player.errorOccurred.connect(lambda _code, message: self.notify('preview.error', {'message': message}))
                self.players[key], self.outputs[key] = player, output
            player = self.players[key]
            url = QUrl.fromLocalFile(track.path) if track.path else QUrl()
            if player.source() != url:
                player.setSource(url)
            self.outputs[key].setVolume(min(1, track.gain * self.volume) if key in active else 0)
        for key, player in self.players.items():
            if key in active and self.playing:
                if player.playbackState() != QMediaPlayer.PlayingState:
                    player.setPosition(self.position)
                    player.play()
            else:
                player.pause()
        return {'ok': True, 'active_roles': list(active)}

    def control(self, data):
        operation = str(data.get('operation', ''))
        if operation == 'volume':
            volume = float(data.get('value', .8))
            if not 0 <= volume <= 1: raise ValueError('音量超出范围')
            self.volume = volume
            for track in self.plan.active_tracks if self.plan else []:
                self.outputs[track.role.value].setVolume(min(1, track.gain * volume))
        elif operation == 'seek':
            self.preview_end = None
            self.position = max(0, int(float(data.get('value', 0))))
            for player in self.players.values(): player.setPosition(self.position)
        elif operation == 'pause':
            self.stop()
        elif operation == 'play':
            if not self.plan or not self.plan.active_tracks: raise ValueError('当前试听模式没有可播放的音轨')
            limit = data.get('value')
            self.preview_end = self.position + 20000 if limit == 20 else None
            self.playing = True
            for track in self.plan.active_tracks:
                player = self.players[track.role.value]
                player.setPosition(self.position)
                player.play()
            self.timer.start()
        else:
            raise ValueError('不支持的试听操作')
        self._tick()
        return {'ok': True}

    def stop(self):
        self.playing = False
        self.timer.stop()
        for player in self.players.values(): player.pause()

    def _tick(self):
        active = self.plan.active_tracks if self.plan else ()
        duration = 0
        if active:
            primary = self.players[active[0].role.value]
            duration = primary.duration() or active[0].duration_ms
            if self.playing:
                self.position = primary.position()
                if getattr(self, 'preview_end', None) is not None and self.position >= self.preview_end:
                    self.stop()
                if primary.mediaStatus() == QMediaPlayer.EndOfMedia:
                    self.stop()
                    self.position = 0
                for track in active[1:]:
                    player = self.players[track.role.value]
                    if abs(player.position() - self.position) > 50: player.setPosition(self.position)
        self.notify('preview.state', {'playing': self.playing, 'position': self.position,
                                     'duration': duration, 'cover_id': self.cover_id})

    def set_project(self, project):
        self.stop()
        self.cover_id = ''
        self.plan = None
        self.position = 0
        super().set_project(project)

    def close(self):
        self.stop()
        for player in self.players.values(): player.setSource(QUrl())
        super().close()
