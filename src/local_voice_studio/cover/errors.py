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


_OOM_MARKERS = (
    "out of memory",
    "cuda error",
    "cuda oom",
    "not enough memory",
    "memoryerror",
)
_DISK_MARKERS = (
    "no space left on device",
    "disk full",
    "insufficient disk",
    "磁盘空间不足",
    "没有剩余空间",
)


def classify_backend_error(message: str, tail: str = "") -> str:
    """Rewrite a backend (FFmpeg/RVC/UVR/SenseVoice) failure with an actionable
    diagnosis when its output matches a known resource condition."""
    combined = (message + "\n" + tail).lower()
    if any(marker in combined for marker in _OOM_MARKERS):
        return "显存（GPU 内存）不足或 CUDA 出错：请关闭其他占用显存的程序后重试；若显存确实有限，可在设置中尝试降低任务负载或重启本地引擎。"
    if any(marker in combined for marker in _DISK_MARKERS):
        return "磁盘空间不足：请清理磁盘空间（建议保留至少 2 GB 可用空间）后重试。"
    return message
