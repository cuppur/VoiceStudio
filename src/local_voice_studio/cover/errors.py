"""Stable error taxonomy for the Cover application boundary."""
from __future__ import annotations


class CoverError(RuntimeError):
    code = "cover.error"
    recoverable = True

    def __init__(self, message: str, *, code: str | None = None, recoverable: bool | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if recoverable is not None:
            self.recoverable = recoverable


class AssetValidationError(CoverError):
    code = "cover.asset_invalid"


class RightsRequiredError(CoverError, PermissionError):
    code = "cover.rights_required"


class ConsentRequiredError(CoverError, PermissionError):
    code = "cover.consent_required"


class ModelNotReadyError(CoverError, PermissionError):
    code = "cover.model_not_ready"


class MixAlignmentError(CoverError):
    code = "cover.mix_alignment"


class RenderCancelledError(CoverError):
    code = "cover.render_cancelled"


class ExportConflictError(CoverError, FileExistsError):
    code = "cover.export_conflict"


def error_payload(exc: Exception) -> dict[str, object]:
    """Turn a CoverError into the compact JSONL error contract."""
    return {
        "code": getattr(exc, "code", "cover.error"),
        "message": str(exc),
        "recoverable": bool(getattr(exc, "recoverable", True)),
    }
