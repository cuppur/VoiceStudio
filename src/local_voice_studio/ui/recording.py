from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices


def analyse_pcm_quality(data: bytes, sample_rate: int = 48000, channels: int = 1) -> dict:
    """Return small, user-facing recording hints without retaining voice data."""
    values = array("h")
    values.frombytes(data[: len(data) // 2 * 2])
    if not values:
        return {"level": 0.0, "peak": 0.0, "clipping": False, "volume": "无声音", "noise": "未知", "environment": "等待录音"}
    stride = max(1, channels)
    mono = values[::stride]
    rms = math.sqrt(sum(value * value for value in mono) / len(mono)) / 32768
    peak = max(abs(value) for value in mono) / 32768
    clipped = sum(1 for value in mono if abs(value) >= 32112) / len(mono)
    volume = "偏低" if rms < .012 else "过高" if rms > .32 or clipped > .001 else "正常"
    # The first short window is only a hint, not a laboratory noise-floor measurement.
    window = mono[: max(1, min(len(mono), sample_rate // 3))]
    floor = math.sqrt(sum(value * value for value in window) / len(window)) / 32768
    noise = "低" if floor < .018 else "中等" if floor < .045 else "较高"
    environment = "很好" if volume == "正常" and noise != "较高" and clipped <= .001 else "建议调整"
    return {"level": min(1.0, rms * 4), "peak": peak, "clipping": clipped > .001, "volume": volume, "noise": noise, "environment": environment}


class Recorder(QObject):
    level_changed = Signal(float)
    quality_changed = Signal(dict)
    stopped = Signal(str, float)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.source: QAudioSource | None = None
        self.device = None
        self.buffer = bytearray()
        self.destination: Path | None = None
        self.format = QAudioFormat()
        self.last_quality: dict = analyse_pcm_quality(b"")

    @staticmethod
    def inputs():
        return QMediaDevices.audioInputs()

    def start(self, destination: Path, device_index: int = 0) -> None:
        devices = self.inputs()
        if not devices:
            self.error.emit("没有检测到麦克风")
            return
        self.destination = destination
        self.buffer.clear()
        self.last_quality = analyse_pcm_quality(b"")
        self.format = QAudioFormat()
        self.format.setSampleRate(48000)
        self.format.setChannelCount(1)
        self.format.setSampleFormat(QAudioFormat.Int16)
        selected = devices[min(max(device_index, 0), len(devices) - 1)]
        if not selected.isFormatSupported(self.format):
            self.format = selected.preferredFormat()
        self.source = QAudioSource(selected, self.format, self)
        self.device = self.source.start()
        if self.device is None:
            self.error.emit("麦克风启动失败")
            self.source = None
            return
        self.device.readyRead.connect(self._read)

    def _read(self) -> None:
        if self.device is None:
            return
        data = bytes(self.device.readAll())
        self.buffer.extend(data)
        if self.format.sampleFormat() == QAudioFormat.Int16 and len(data) >= 2:
            quality = analyse_pcm_quality(data, self.format.sampleRate(), self.format.channelCount())
            self.last_quality = quality
            self.level_changed.emit(float(quality["level"]))
            self.quality_changed.emit(quality)

    def stop(self) -> None:
        if self.source is None or self.destination is None:
            return
        self._read()
        self.source.stop()
        destination = self.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.format.sampleFormat() != QAudioFormat.Int16:
            self.error.emit("当前麦克风不支持 16 位 PCM，录音未保存")
        else:
            channels = self.format.channelCount()
            rate = self.format.sampleRate()
            with wave.open(str(destination), "wb") as stream:
                stream.setnchannels(channels)
                stream.setsampwidth(2)
                stream.setframerate(rate)
                stream.writeframes(self.buffer)
            duration = len(self.buffer) / max(1, rate * channels * 2)
            self.last_quality = analyse_pcm_quality(bytes(self.buffer), rate, channels)
            self.quality_changed.emit(self.last_quality)
            self.stopped.emit(str(destination), duration)
        self.source.deleteLater()
        self.source = None
        self.device = None
        self.destination = None
