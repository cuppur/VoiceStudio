import threading
import time
from local_voice_studio.application.jobs.scheduler import GpuJobScheduler, SchedulerStatus

def test_scheduler_serializes_and_records():
    active=[]; lock=threading.Lock()
    def runner(task, cancelled):
        with lock: active.append(task.kind); assert len(active)==1
        time.sleep(.02)
        with lock: active.pop()
        return task.kind
    s=GpuJobScheduler(runner)
    a=s.submit("a"); b=s.submit("b")
    for _ in range(100):
        if len(s.history())==2: break
        time.sleep(.01)
    assert [x.status for x in s.history()] == [SchedulerStatus.SUCCEEDED, SchedulerStatus.SUCCEEDED]
    assert a.result=="a" and b.result=="b"

def test_scheduler_cancel_queued_and_running():
    gate=threading.Event()
    def runner(task, cancelled):
        while not gate.is_set():
            if cancelled(): raise RuntimeError("cancelled")
            time.sleep(.005)
    s=GpuJobScheduler(runner); a=s.submit("a"); b=s.submit("b")
    assert s.cancel(b.id)
    assert s.cancel(a.id)
    gate.set()
    for _ in range(100):
        if len(s.history())==2: break
        time.sleep(.01)
    assert {x.status for x in s.history()} == {SchedulerStatus.CANCELLED}
