from local_voice_studio.application.jobs import JobStage, ProductJob, ProductJobStatus
from local_voice_studio.application.jobs.controller import CoverPipelineController

def test_controller_forwards_worker_events():
    names=["analyze","waveform","separation","lyrics","voice_conversion","post_process","mix","export"]
    job=ProductJob("ai_cover",{},[JobStage(n,[names[i-1]] if i else []) for i,n in enumerate(names)])
    sent=[]; saved=[]
    c=CoverPipelineController(job, lambda cmd,payload: sent.append((cmd,payload["stage"])) or str(len(sent)), saved.append)
    try: c.complete_local("analyze")
    except RuntimeError: pass
    try: c.complete_local("waveform")
    except RuntimeError: pass
    assert sent==[("separate_song","separation")]
    assert c.handle("1","result",{"output_path":"vocal.wav"}) == "lyrics"
    assert sent[-1]==("transcribe_lyrics","lyrics")
    assert saved and job.stage("separation").status == ProductJobStatus.SUCCEEDED
