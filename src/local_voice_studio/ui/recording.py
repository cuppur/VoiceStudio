from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices


class Recorder(QObject):
    level_changed = Signal(float)
    stopped = Signal(str, float)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.source: QAudioSource | None = None
        self.device = None
        self.buffer = bytearray()
        self.destination: Path | None = None
        self.format = QAudioFormat()

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
            values = array("h")
            values.frombytes(data[: len(data) // 2 * 2])
            if values:
                rms = math.sqrt(sum(value * value for value in values) / len(values)) / 32768
                self.level_changed.emit(min(1.0, rms * 4))

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
            self.stopped.emit(str(destination), duration)
        self.source.deleteLater()
        self.source = None
        self.device = None
        self.destination = None

