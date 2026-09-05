from local_voice_studio.application.jobs import JobStage, ProductJob, ProductJobStatus
from local_voice_studio.application.jobs.pipeline import CoverPipeline

def test_cover_pipeline_dispatches_in_order():
    sent=[]
    names=["analyze","waveform","separation","lyrics","voice_conversion","post_process","mix","export"]
    job=ProductJob("ai_cover", {"project_path":"p"}, [JobStage(n,[names[i-1]] if i else []) for i,n in enumerate(names)])
    pipe=CoverPipeline.create(job, lambda c,p: sent.append((c,p["stage"])) or str(len(sent)))
    try: pipe.dispatch_next()
    except RuntimeError: pass
    else: assert False
    assert sent == []
    try: pipe.complete_local_stage("analyze")
    except RuntimeError: pass
    else: assert False
    assert pipe.complete_local_stage("waveform") == "separation"
    assert sent[-1][0] == "separate_song"

def test_cover_pipeline_missing_stage_rejected():
    try: CoverPipeline.create(ProductJob("wrong", {}, []), lambda *_: "x")
    except ValueError: pass
    else: assert False
