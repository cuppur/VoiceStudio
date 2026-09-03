"""Shared visual tokens for the native VoiceStudio theme."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DesignTokens:
    background: str = "#090B10"
    surface: str = "#111620"
    surface_hover: str = "#191D26"
    border: str = "#222A39"
    text_primary: str = "#F6F8FC"
    text_secondary: str = "#9299A8"
    accent: str = "#5968E8"
    success: str = "#A3D9B1"
    warning: str = "#EBCB8B"
    error: str = "#FFB4B4"
    radius: int = 8
    control_height: int = 32
    spacing: int = 12


TOKENS = DesignTokens()


def qss_values(tokens: DesignTokens = TOKENS) -> dict[str, str]:
    return {
        "@color.background": tokens.background,
        "@color.surface": tokens.surface,
        "@color.surface_hover": tokens.surface_hover,
        "@color.border": tokens.border,
        "@color.text_primary": tokens.text_primary,
        "@color.text_secondary": tokens.text_secondary,
        "@color.accent": tokens.accent,
        "@color.success": tokens.success,
        "@color.warning": tokens.warning,
        "@color.error": tokens.error,
        "@radius.control": f"{tokens.radius}px",
        "@height.control": f"{tokens.control_height}px",
        "@spacing.base": f"{tokens.spacing}px",
    }


__all__ = ["DesignTokens", "TOKENS", "qss_values"]
