"""Headless workflow services used by the HTML shell."""
from __future__ import annotations

from .base import WebService
from .cover import CoverService

__all__ = ["WebService", "CoverService"]
