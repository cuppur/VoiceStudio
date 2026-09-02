"""Domain mixing package.

The public imports remain compatible with the former ``cover.mixing`` module.
"""
from .models import CoverMixSettings, GainScale, MixInput
from .service import CoverMixer
from .backend import AudioRenderResult, FFmpegMixBackend
from ...audio import probe_audio
import subprocess

__all__ = ["AudioRenderResult", "CoverMixSettings", "GainScale", "CoverMixer", "FFmpegMixBackend", "MixInput"]
