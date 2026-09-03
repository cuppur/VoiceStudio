"""Export domain package, preserving the legacy CoverExporter import."""
from .models import ExportFormat, ExportRequest, OverwritePolicy
from .service import CoverExporter
from .backend import ExportBackend, FFmpegExportBackend
from .validation import ExportOutputValidator, ValidatedExportAudio
from ...runtime import EngineRuntimeResolver
import subprocess

__all__ = ["CoverExporter", "ExportBackend", "ExportFormat", "ExportRequest", "FFmpegExportBackend", "OverwritePolicy", "ExportOutputValidator", "ValidatedExportAudio"]
