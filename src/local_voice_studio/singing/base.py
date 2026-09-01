from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class EngineReadiness:
    ready: bool
    errors: tuple[str, ...] = ()
    details: Mapping[str, Any] | None = None


class SingingEngine(Protocol):
    """Small process-oriented contract; implementations must not import torch."""

    def readiness(self) -> EngineReadiness: ...

    def train(self, payload: Mapping[str, Any], cancel: Any = None) -> Sequence[Path]: ...

    def convert(self, payload: Mapping[str, Any], cancel: Any = None) -> Path: ...
