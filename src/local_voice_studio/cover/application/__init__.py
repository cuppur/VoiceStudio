"""Application-layer orchestration for Cover workflows.

The application layer is the only place where UI intent is translated into
worker commands.  Domain and infrastructure modules remain usable without Qt.
"""

from .commands import (
    ExportCoverCommand, PrepareAIVocalCommand, PrepareRenderCommand,
    PrepareSeparationCommand, SeparateSongCommand, ConvertVocalCommand,
    RenderCoverCommand,
)
from .results import (CoverStateResult, OperationResult, SeparateSongResult,
                      ConvertVocalResult, RenderCoverResult, ExportCoverResult)
from .service import CoverApplicationService

__all__ = [
    "CoverApplicationService", "PrepareSeparationCommand",
    "PrepareAIVocalCommand", "PrepareRenderCommand", "ExportCoverCommand",
    "SeparateSongCommand", "ConvertVocalCommand", "RenderCoverCommand",
    "CoverStateResult", "OperationResult", "SeparateSongResult",
    "ConvertVocalResult", "RenderCoverResult", "ExportCoverResult",
]
