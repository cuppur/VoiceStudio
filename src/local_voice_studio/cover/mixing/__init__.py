"""Domain mixing package.

The public imports remain compatible with the former ``cover.mixing`` module.
"""
from .models import CoverMixSettings, GainScale, MixInput
from ..models import ContentOrigin, CoverAssetRole
from .service import CoverMixService, CoverMixer
from .backend import AudioRenderResult, FFmpegMixBackend, MixBackend
from ...audio import probe_audio
from .validation import (
    AudioInfo,
    MixAlignmentReport,
    MixAlignmentValidator,
    MixValidator,
    ResolvedAudioAsset,
    ResolvedMixInputs,
)

__all__ = [
    "AudioInfo", "AudioRenderResult", "ContentOrigin", "CoverAssetRole",
    "CoverMixService", "CoverMixSettings", "CoverMixer", "FFmpegMixBackend",
    "GainScale", "MixAlignmentReport", "MixAlignmentValidator", "MixBackend",
    "MixInput", "MixValidator", "ResolvedAudioAsset", "ResolvedMixInputs",
]
