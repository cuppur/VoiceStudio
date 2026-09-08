from local_voice_studio.application.jobs import JobStage, ProductJobStatus
from local_voice_studio.application.jobs.coordinator import JobCoordinator


def test_job_coordinator_persists_worker_like_events(tmp_path):
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    paths = AppPaths(tmp_path / "home", tmp_path / "projects", tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3")
    coordinator = JobCoordinator(StudioStore(paths))
    job = coordinator.start("separation", {"cover_id": "c"}, ["separation"])
    coordinator.handle_progress(job.id, "separation", .4, "running")
    completed = coordinator.handle_result(job.id, "separation", ["vocal", "instrumental"])
    assert completed.status == ProductJobStatus.SUCCEEDED
    assert coordinator.store.load_product_job(job.id).stage("separation").outputs == ["vocal", "instrumental"]


def test_job_coordinator_marks_recoverable_error(tmp_path):
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    paths = AppPaths(tmp_path / "home", tmp_path / "projects", tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3")
    coordinator = JobCoordinator(StudioStore(paths))
    job = coordinator.start("separation", {}, ["separation"])
    failed = coordinator.handle_error(job.id, "模型缺失")
    assert failed.status == ProductJobStatus.RECOVERABLE
from local_voice_studio.application.jobs import JobStage, ProductJob


def test_mark_cancelling_persists_state(tmp_path):
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    from local_voice_studio.application.jobs.coordinator import JobCoordinator
    paths = AppPaths(tmp_path / "home", tmp_path / "projects", tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3")
    coordinator = JobCoordinator(StudioStore(paths))
    job = coordinator.start("separation", {}, ["separation"])
    assert coordinator.mark_cancelling(job.id).status.value == "cancelling"
