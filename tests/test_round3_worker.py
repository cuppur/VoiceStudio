from local_voice_studio.protocol import Message
from test_worker_phase4 import service


def test_worker_keeps_product_job_context_and_finishes(tmp_path):
    worker = service(tmp_path)
    worker._train = lambda rid, payload: worker.emit(rid, 'result', {'outputs': []})
    worker.handle(Message('train', {}, 'training'))
    worker.current_thread.join(3)
    jobs = worker.job_coordinator.store.list_product_jobs()
    assert len(jobs) == 1
    assert jobs[0].status.value == 'succeeded'
