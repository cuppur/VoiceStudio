from pathlib import Path


def load_theme() -> str:
    return (Path(__file__).with_name("studio.qss")).read_text(encoding="utf-8")

