"""Options for non-interactive helper processes (never steal desktop focus)."""
import os
import subprocess


def hidden_process_options() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
