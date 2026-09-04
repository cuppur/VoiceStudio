import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_sessionfinish(session, exitstatus):
    # Destroy every widget tree (and its QThread/QMediaPlayer/QAudioOutput
    # children) while the QApplication is still alive, so Python interpreter
    # teardown never destroys Qt objects after the app — that ordering is a
    # known PySide6 "QThreadStorage destroyed before end of thread" abort.
    # We deliberately leave the QApplication itself for normal teardown.
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for widget in list(app.topLevelWidgets()):
                widget.deleteLater()
            app.processEvents()
            app.quit()
    except Exception:
        pass
