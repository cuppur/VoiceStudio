"""Pure export request models."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

class ExportFormat(str, Enum):
    WAV = "wav"
    MP3 = "mp3"
    BOTH = "both"

class OverwritePolicy(str, Enum):
    REJECT = "reject"
    REPLACE = "replace"

@dataclass(frozen=True)
class ExportRequest:
    format: ExportFormat
    file_name: str
    destination: Path
    overwrite_policy: OverwritePolicy
    publication_rights_acknowledged: bool
