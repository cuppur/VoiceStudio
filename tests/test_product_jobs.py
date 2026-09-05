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
