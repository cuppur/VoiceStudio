"""Persistent data primitives for AI cover projects."""

from .project import CoverAsset, CoverProject, CoverProjectError, content_origin
from .models import CoverAssetRole, ContentOrigin
from .mixing import CoverMixSettings, CoverMixer, GainScale
from .exporting import CoverExporter

__all__ = ["CoverAsset", "CoverProject", "CoverProjectError", "content_origin", "CoverAssetRole", "ContentOrigin", "CoverMixSettings", "GainScale", "CoverMixer", "CoverExporter"]
