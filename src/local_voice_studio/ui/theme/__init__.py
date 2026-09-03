from pathlib import Path
from .tokens import TOKENS, qss_values


def load_theme() -> str:
    theme = (Path(__file__).with_name("studio.qss")).read_text(encoding="utf-8")
    for placeholder, value in qss_values(TOKENS).items():
        theme = theme.replace(placeholder, value)
    return theme
