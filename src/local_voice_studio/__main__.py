import sys


if __name__ == "__main__":
    if "--worker" in sys.argv:
        from .worker import main
    else:
        from .app import main
    raise SystemExit(main())
