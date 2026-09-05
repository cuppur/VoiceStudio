from local_voice_studio.application.jobs import JobStage, ProductJob, ProductJobStatus


def test_job_stages_enforce_dependencies_and_recover_round_trip():
    job = ProductJob("ai_cover", {}, [JobStage("analyze"), JobStage("separation", ["analyze"]), JobStage("mix", ["separation"])])
    assert [stage.name for stage in job.ready_stages()] == ["analyze"]
    job.mark_stage_running("analyze")
    job.mark_stage_succeeded("analyze", ["analysis"])
    assert [stage.name for stage in job.ready_stages()] == ["separation"]
    job.mark_stage_running("separation")
    job.mark_stage_succeeded("separation", ["vocal", "instrumental"])
    restored = ProductJob.from_dict(job.to_dict())
    assert restored.status == ProductJobStatus.RUNNING
    assert restored.ready_stages()[0].name == "mix"


def test_job_cancel_is_explicit():
    job = ProductJob("separation", {}, [JobStage("separation")])
    job.cancel(); assert job.status == ProductJobStatus.CANCELLING
    job.mark_cancelled(); assert job.status == ProductJobStatus.CANCELLED
from local_voice_studio.application.jobs import JobStage, ProductJob


def test_store_persists_product_job_state(tmp_path):
    from local_voice_studio.paths import AppPaths
    from local_voice_studio.storage import StudioStore
    paths = AppPaths(tmp_path / "home", tmp_path / "projects", tmp_path / "runtime", tmp_path / "engine", tmp_path / "models", tmp_path / "logs", tmp_path / "db.sqlite3")
    store = StudioStore(paths)
    job = ProductJob("ai_cover", {"project": "p1"}, [JobStage("analyze")])
    store.save_product_job(job)
    restored = store.load_product_job(job.id)
    assert restored.id == job.id and restored.kind == "ai_cover"
    assert store.list_product_jobs()[0].id == job.id


def test_recoverable_job_can_retry_current_stage():
    job = ProductJob("ai_cover", {}, [JobStage("separation")])
    job.mark_stage_running("separation")
    job.status = ProductJobStatus.RECOVERABLE
    job.error = "temporary"
    job.stage("separation").error = "temporary"
    job.retry()
    assert job.status == ProductJobStatus.QUEUED
    assert job.stage("separation").status == ProductJobStatus.QUEUED
    assert job.error == ""
