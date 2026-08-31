from __future__ import annotations
from pathlib import Path
from PySide6.QtCore import QThread, Signal, Qt, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton, QSpinBox, QSlider, QVBoxLayout, QWidget, QCheckBox
from .cover_session import SongSession, parse_lrc
from .studio_widgets import LyricView, StemTrackWidget, TaskProgress, TransportWidget, VoiceSelector, WaveformWidget

class _LoadThread(QThread):
    loaded = Signal(object); failed = Signal(str)
    def __init__(self, audio, lrc, paths, parent=None): super().__init__(parent); self.audio,self.lrc,self.paths=Path(audio),lrc,paths
    def run(self):
        try: self.loaded.emit(SongSession.load(self.audio,lrc_path=self.lrc,paths=self.paths,cache_dir=self.paths.data_root/'cache'/'waveforms',cancel=self.isInterruptionRequested))
        except Exception as e: self.failed.emit(str(e))

class _TaskDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent); self.setWindowTitle('AI 翻唱任务'); b=QVBoxLayout(self); b.addWidget(QLabel('AI翻唱引擎尚未安装，将在下一阶段启用')); self.progress=TaskProgress(); b.addWidget(self.progress)
        for i,t in enumerate(('读取歌曲','处理歌词','准备目标声音','生成翻唱','导出混音')): b.addWidget(QLabel(f'{i+1}. {t} · 等待'))
        x=QDialogButtonBox(QDialogButtonBox.Close); x.rejected.connect(self.reject); b.addWidget(x)

class _SeparationDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent); self.setWindowTitle('分离人声与伴奏'); b=QVBoxLayout(self); b.addWidget(QLabel('选择分离模式'))
        for t in ('快速 UVR5 · 已安装','高质量分离 · 即将支持','多轨分离 · 即将支持'): b.addWidget(QLabel('●  '+t))
        x=QDialogButtonBox(QDialogButtonBox.Close); x.rejected.connect(self.reject); b.addWidget(x)

class CoverPage(QWidget):
    profileChanged=Signal(str)
    def __init__(self,paths,store,project,parent=None):
        super().__init__(parent); self.paths,self.store,self.project=paths,store,Path(project); self.session=None; self._thread=None
        self.audio_output=QAudioOutput(self); self.audio_output.setVolume(.75); self.player=QMediaPlayer(self); self.player.setAudioOutput(self.audio_output); self._build(); self.refresh_profiles()
        self.player.positionChanged.connect(self._position); self.player.durationChanged.connect(self._duration); self.player.playbackStateChanged.connect(lambda s:self.transport.set_playing(s==QMediaPlayer.PlayingState)); self.player.mediaStatusChanged.connect(self._media_status)
    def _build(self):
        self.setStyleSheet("#coverPage{background:#0b1020;color:#e5e7eb} QLabel#title{font-size:28px;font-weight:700;color:#fff} QLabel#muted{color:#8b95a7} QFrame#settings{background:#151c2f;border:1px solid #283452;border-radius:14px} QPlainTextEdit,QComboBox,QSpinBox{background:#121a2c;color:#e5e7eb;border:1px solid #33415f;border-radius:7px;padding:7px} QPushButton{background:#1b2640;color:#e5e7eb;border:1px solid #3b4d76;border-radius:7px;padding:9px 14px} QPushButton#primaryButton{background:#7c3aed;color:#fff}"); root=QHBoxLayout(self); root.setContentsMargins(28,24,28,24); root.setSpacing(22); left=QVBoxLayout(); root.addLayout(left,1)
        h=QHBoxLayout(); self.song_title=QLabel('还没有导入歌曲'); self.song_title.setObjectName('title'); h.addWidget(self.song_title); h.addStretch(); self.import_button=QPushButton('导入歌曲'); self.import_button.clicked.connect(self.import_song); h.addWidget(self.import_button); left.addLayout(h); self.song_meta=QLabel('支持 WAV / MP3 / FLAC · 导入后自动寻找同名 LRC'); self.song_meta.setObjectName('muted'); left.addWidget(self.song_meta)
        self.waveform=WaveformWidget(); self.waveform.setMinimumHeight(130); self.waveform.setMaximumHeight(170); self.waveform.seek_requested.connect(self.player.setPosition); left.addWidget(self.waveform); self.transport=TransportWidget(); self.transport.play_requested.connect(self.toggle_playback); self.transport.seek_relative_requested.connect(lambda n:self.player.setPosition(max(0,min(self.player.duration(),self.player.position()+n)))); self.transport.timeline.sliderMoved.connect(self.player.setPosition); self.transport.volume_changed.connect(lambda n:self.audio_output.setVolume(n/100)); left.addWidget(self.transport)
        stems=QVBoxLayout(); self.stems=[]
        for n,s in (('原曲','Ready'),('人声','Empty'),('伴奏','Empty'),('音高','Empty'),('混音','Empty')): w=StemTrackWidget(n); w.set_status(s); self.stems.append(w); stems.addWidget(w)
        left.addLayout(stems); lh=QHBoxLayout(); lh.addWidget(QLabel('歌词')); lh.addStretch(); self.lyric_status=QLabel('未载入'); self.lyric_status.setObjectName('muted'); lh.addWidget(self.lyric_status); self.lyric_import=QPushButton('手动导入 LRC'); self.lyric_import.clicked.connect(self.import_lyrics); lh.addWidget(self.lyric_import); left.addLayout(lh); self.lyrics=LyricView(); self.lyrics.seek_requested.connect(self.player.setPosition); left.addWidget(self.lyrics,1)
        a=QHBoxLayout(); self.separate_button=QPushButton('分离人声 / 伴奏'); self.separate_button.clicked.connect(lambda:_SeparationDialog(self).exec()); a.addWidget(self.separate_button); a.addStretch(); self.cover_button=QPushButton('开始 AI 翻唱'); self.cover_button.setObjectName('primaryButton'); self.cover_button.clicked.connect(lambda:_TaskDialog(self).exec()); a.addWidget(self.cover_button); left.addLayout(a)
        panel=QFrame(); panel.setObjectName('settings'); panel.setFixedWidth(270); f=QFormLayout(panel); f.addRow(QLabel('翻唱设置')); self.profile_combo=VoiceSelector(); self.profile_combo.voice_selected.connect(lambda p:self.profileChanged.emit(str(p or ''))); f.addRow('目标声音',self.profile_combo); self.pitch=QSpinBox(); self.pitch.setRange(-12,12); self.pitch.setValue(0); self.pitch.setSuffix(' 半音'); f.addRow('音调',self.pitch)
        for label,val,name in (('音色',75,'timbre'),('细节',50,'detail')): x=QSlider(Qt.Horizontal); x.setRange(0,100); x.setValue(val); setattr(self,name,x); f.addRow(label,x)
        for label,name in (('AI人声','ai_gain'),('伴奏','inst_gain')): x=QSpinBox(); x.setRange(-24,24); x.setValue(0); x.setSuffix(' dB'); setattr(self,name,x); f.addRow(label,x)
        for label,name in (('自动音高','auto_pitch'),('去混响','dereverb'),('保留和声','keep_harmony')): x=QCheckBox(label); x.setChecked(True); setattr(self,name,x); f.addRow(x)
        f.addRow(QLabel('本地处理 · 不上传音频')); root.addWidget(panel)
    def import_song(self):
        p,_=QFileDialog.getOpenFileName(self,'导入歌曲',str(self.project),'音频 (*.wav *.flac *.mp3 *.m4a *.aac *.ogg)');
        if p:self.set_song(p)
    def set_song(self,path):
        if self._thread and self._thread.isRunning(): return
        self.player.stop(); self.player.setSource(QUrl()); p=Path(path); self.song_path=str(p); self.song_title.setText(p.stem); self.song_meta.setText(f'{p.suffix.upper().lstrip(".")} · 读取中…'); self.import_button.setEnabled(False); self._thread=_LoadThread(p,p.with_suffix('.lrc'),self.paths,self); self._thread.loaded.connect(self._loaded); self._thread.failed.connect(lambda e:self.song_meta.setText('读取失败：'+e)); self._thread.finished.connect(self._load_finished); self._thread.start()
    def _load_finished(self):
        self.import_button.setEnabled(True); self._thread=None
    def _loaded(self,s):
        self.session=s; m=s.metadata; self.player.setSource(QUrl.fromLocalFile(s.audio_path)); self.waveform.set_waveform(s.peaks,int(m.duration_seconds*1000)); self.lyrics.set_lyrics(s.lyrics); self.song_meta.setText(f'{Path(s.audio_path).suffix.upper().lstrip(".")} · {m.duration_seconds:.1f}s · {m.sample_rate} Hz · {m.channels}ch · 目标声音：{self.profile_combo.currentText()}'); self.lyric_status.setText('已匹配同名 LRC' if s.lyrics else '未载入')
    def import_lyrics(self):
        p,_=QFileDialog.getOpenFileName(self,'导入歌词',str(self.project),'歌词 (*.lrc *.txt)');
        if p:self.lyrics.set_lyrics(parse_lrc(Path(p).read_text(encoding='utf-8-sig',errors='replace'))); self.lyric_status.setText('已手动导入')
    def toggle_playback(self): self.player.pause() if self.player.playbackState()==QMediaPlayer.PlayingState else self.player.play()
    def _position(self,v): self.waveform.set_position(v); self.lyrics.set_position(v); self.transport.set_timeline(v,self.player.duration())
    def _duration(self,v): self.waveform.set_waveform(self.session.peaks if self.session else [],v); self.transport.set_timeline(self.player.position(),v)
    def _media_status(self,s):
        if s==QMediaPlayer.EndOfMedia:self.player.setPosition(0); self.transport.set_playing(False)
    def refresh_profiles(self):
        try:self.profile_combo.set_profiles(self.store.list_profiles(self.project))
        except (AttributeError,OSError,TypeError): self.profile_combo.set_profiles([])
    def release_resources(self):
        if self._thread and self._thread.isRunning(): self._thread.requestInterruption(); self._thread.wait()
        self.player.stop(); self.player.setSource(QUrl()); self.player.setAudioOutput(None)
    def closeEvent(self,e): self.release_resources(); super().closeEvent(e)
