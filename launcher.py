"""PyInstaller-safe entry point for both the desktop UI and worker mode."""

import sys


if "--worker" in sys.argv:
    from local_voice_studio.worker import main
else:
    from local_voice_studio.app import main


if __name__ == "__main__":
    raise SystemExit(main())

