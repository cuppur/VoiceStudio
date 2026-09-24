"""Exercise persisted cover stages and non-destructive Take selection."""
from hashlib import sha256
from uuid import uuid4

from local_voice_studio.application.jobs import ProductJob, ProductJobStatus
from local_voice_studio.cover.project import CoverAsset, CoverProject
from local_voice_studio.models import VoiceProfile
from local_voice_studio.ui.web.data import StudioSnapshot
from local_voice_studio.ui.web.services.cover import CoverService
from test_web_services import _fixture, _wav


def test_one_click_cover_resumes_failed_middle_stage(tmp_path, monkeypatch):
    paths, store, project, worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path/'song.wav'))['cover_id']
    service.attest_rights(cover_id)
    profile = VoiceProfile('歌唱声音', True)
    profile.active_singing_model_id = uuid4().hex
    store.save_profile(project, profile)
    sent = []
    def stage(name):
        def send(*_args, **_kwargs):
            rid = f'req-{len(sent)+1}'
            sent.append((rid,name))
            service._tasks[rid] = {'job_id':service.coordinator.start(name,{},[name]).id,
                                   'kind':name,'stage':name,'title':name,
                                   'cover_id':cover_id,'profile_id':profile.id,'on_result':None}
            return {'request_id':rid,'job_id':'child'}
        return send
    monkeypatch.setattr(service,'separate',stage('separation'))
    monkeypatch.setattr(service,'convert_vocal',stage('voice_conversion'))
    monkeypatch.setattr(service,'render',stage('mix'))
    started = service.start_cover(cover_id,profile.id)
    assert started['job_id'] != 'child' and sent[-1][1] == 'separation'
    service.handle_worker_event(sent[-1][0],'result',{})
    assert sent[-1][1] == 'voice_conversion'
    failed = sent[-1][0]
    service.handle_worker_event(failed,'error',{'message':'模拟显存不足'})
    assert store.load_product_job(started['job_id']).status.value == 'recoverable'
    service._tasks.clear()
    service.resume_cover(started['job_id'])
    assert sent[-1][1] == 'voice_conversion'
    service.handle_worker_event(sent[-1][0],'result',{})
    assert sent[-1][1] == 'mix'
    service.handle_worker_event(sent[-1][0],'result',{})
    assert store.load_product_job(started['job_id']).status.value == 'succeeded'
    assert [s.status.value for s in store.load_product_job(started['job_id']).stages] == ['succeeded']*3


def test_take_restore_changes_selected_song_and_survives_reload(tmp_path):
    paths, store, project, worker, service = _fixture(tmp_path)
    cover_id = service.import_song(_wav(tmp_path/'song.wav'))['cover_id']
    cover = CoverProject.load(project,cover_id)
    for n in (1,2):
        path = _wav(cover.root/f'output/take{n}.wav')
        cover.add_asset(CoverAsset(f'take{n}','final_mix',f'output/take{n}.wav',
                                   sha256(path.read_bytes()).hexdigest(),'ai_generated','test',
                                   metadata={'settings':{'ai_gain_db':n}}))
    assert service.state(cover_id)['active_take_id'] == 'take2'
    service.select_take(cover_id,'take1')
    assert CoverProject.load(project,cover_id).get_asset(role='final_mix').id == 'take1'
    assert service.state(cover_id)['final_asset_id'] == 'take1'


def test_task_snapshot_does_not_hide_resumable_training_product_job(tmp_path):
    paths, store, project, _worker, _service = _fixture(tmp_path)
    job = ProductJob('voice_training', {
        'project_path': str(project), 'workflow_id': 'workflow-1',
    }, [])
    job.status = ProductJobStatus.FAILED
    store.save_product_job(job)

    row = next(item for item in StudioSnapshot(paths, store, project).tasks() if item['id'] == job.id)

    assert row['workflow_id'] == 'workflow-1'
    assert row['resumable'] is True
