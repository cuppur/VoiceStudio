"""QtWebEngine-based HTML shell for VoiceStudio.

The shell renders the approved v4 prototype markup verbatim and exposes a
narrow ``bridge`` object to the page through QWebChannel.  Every value the page
shows comes from :mod:`local_voice_studio.ui.web.data`; nothing is fabricated.
"""
from __future__ import annotations

from .bridge import StudioBridge
from .shell import WebStudioWindow

__all__ = ["StudioBridge", "WebStudioWindow"]
