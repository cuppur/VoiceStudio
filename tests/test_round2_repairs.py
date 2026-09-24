"""Regression coverage for automatic training and desktop responsiveness."""
import json
import os
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from local_voice_studio import workflow as workflow_module
from local_voice_studio.models import DatasetDraft, DatasetDraftSegment, DatasetManifest, TrainingWorkflow, VoiceProfile, WorkflowStage, WorkflowStatus, dataset_snapshot_sha256
from local_voice_studio.audio import sha256_file
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.ui.web.services.training import TrainingService
from local_voice_studio.infrastructure.process_options import hidden_process_options
from test_web_services import _fixture, _wav, _app


def spin(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('background operation did not complete')


def test_preparation_continues_automatically_without_blocking_ui(tmp_path, monkeypatch):
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('自动训练', True, consent_record='user consent', consent_confirmed_at='2026-09-09')
    store.save_profile(project, profile)
    controller = service.controller
    ticks = []
    timer = QTimer(); timer.timeout.connect(lambda: ticks.append(time.monotonic())); timer.start(10)
    workflow = controller.start(profile, [], auto_continue=True)
    source = _wav(project / 'segment.wav', 6)
    segments = [DatasetDraftSegment(source.name, 0, 6, text='这是训练文本') for _ in range(10)]
    segments.append(DatasetDraftSegment(source.name, 0, 6, text='异常片段', quality_flags=['clipping_risk'], included=True))
    draft = DatasetDraft(workflow.id, profile.id, workflow.preparation_id, segments)
    def prepare(*args):
        time.sleep(.25)
        return draft
    monkeypatch.setattr(workflow_module, 'draft_from_preparation', prepare)
    request = next(iter(controller.requests))
    start = time.monotonic()
    controller._on_event(request, 'result', {'outputs': ['manifest.json']})
    assert time.monotonic() - start < .2
    spin(lambda: any(value[1] == 'features' for value in controller.requests.values()))
    timer.stop()
    assert len(ticks) >= 5
    stored = store.load_draft(project, draft.id)
    assert stored.confirmed_seconds == 60
    assert all(s.auto_accepted and not s.human_confirmed for s in stored.segments[:10])
    assert not stored.segments[-1].included
    assert store.load_workflow(project, workflow.id).dataset_snapshot_id
    service.close()


def test_cancel_during_background_scan_does_not_start_training(tmp_path, monkeypatch):
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('取消', True); store.save_profile(project, profile)
    workflow = service.controller.start(profile, [], auto_continue=True)
    def prepare(*args):
        time.sleep(.1)
        return DatasetDraft(workflow.id, profile.id, workflow.preparation_id, [])
    monkeypatch.setattr(workflow_module, 'draft_from_preparation', prepare)
    service.controller._on_event(next(iter(service.controller.requests)), 'result', {'outputs':['manifest.json']})
    service.controller.cancel(workflow)
    spin(lambda: not service.controller.background)
    assert not service.controller.requests
    assert store.load_workflow(project, workflow.id).status == WorkflowStatus.CANCELLED
    service.close()


def test_song_delete_moves_only_managed_copy_and_blocks_active_job(tmp_path):
    paths, store, project, worker, cover = _fixture(tmp_path)
    source = _wav(tmp_path / '保留原件.wav', 1)
    created = cover.import_song(source)
    cover_id = created['cover_id']
    cover._tasks['running'] = {'cover_id':cover_id, 'kind':'separation'}
    with pytest.raises(ValueError, match='正在处理'):
        cover.delete_song(cover_id)
    cover._tasks.clear()
    cover.delete_song(cover_id)
    assert source.is_file()
    assert not CoverProject.list(project)
    assert (project / '.trash' / 'songs' / cover_id / 'manifest.json').is_file()


def test_engine_status_does_not_read_model_contents(tmp_path, monkeypatch):
    from local_voice_studio.cover import separation
    paths, store, project, worker, cover = _fixture(tmp_path)
    model = paths.models_root / 'separation' / separation.ROFORMER_MODEL_NAME
    model.parent.mkdir(parents=True); model.write_bytes(b'fake')
    monkeypatch.setattr(separation, 'ROFORMER_MODEL_SIZE', 4)
    pin = {'id':'roformer-vocals-onnx','size':4,'sha256':separation.ROFORMER_MODEL_SHA256}
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda p,*a,**kw: json.dumps({'installed_file_pins':[pin]}) if p.name=='runtime-assets-v1.json' else original(p,*a,**kw))
    monkeypatch.setattr(separation, 'sha256_file', lambda p: pytest.fail('UI read the model contents'))
    assert cover.engines()['roformer']['ready']


def test_audio_helper_processes_are_hidden():
    import subprocess
    options = hidden_process_options()
    assert options == ({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {})


def test_old_review_task_uses_current_training_options(tmp_path, monkeypatch):
    from local_voice_studio.models import TrainingWorkflow, WorkflowStage
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('旧草稿', True); store.save_profile(project, profile)
    workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.REVIEW_REQUIRED)
    draft = DatasetDraft(workflow.id, profile.id, 'old', [])
    workflow.draft_id = draft.id
    store.save_draft(project, draft); store.save_workflow(project, workflow)
    calls = []
    monkeypatch.setattr(service.controller, 'confirm_and_train', lambda w,d,**kw:calls.append((w,kw)))
    service.resume(workflow.id, options={'quality':'quick','train_singing':True})
    assert calls[0][1]['automatic']
    assert calls[0][0].processing_options['quality'] == 'quick'
    assert calls[0][0].processing_options['train_singing'] is True
    service.close()


def test_cancelled_training_resume_starts_a_new_worker_attempt(tmp_path):
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('恢复训练', True, consent_record='user consent', consent_confirmed_at='2026-09-09')
    store.save_profile(project, profile)
    dataset_folder = project / 'datasets' / 'resume-snapshot'
    dataset_folder.mkdir(parents=True)
    list_path = dataset_folder / 'dataset.list'
    list_path.write_text('', encoding='utf-8')
    dataset = DatasetManifest('frozen', profile.id, id='resume-snapshot', frozen=True,
                              list_path=str(list_path), wav_dir=str(dataset_folder / 'audio'),
                              list_relative_path='datasets/resume-snapshot/dataset.list',
                              list_sha256=sha256_file(list_path))
    dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
    store.save_dataset_snapshot(project, dataset)
    feature_dir = paths.data_root / 'training' / profile.id / dataset.snapshot_sha256 / 'features'
    feature_dir.mkdir(parents=True)
    phoneme = feature_dir / '2-name2text.txt'
    semantic = feature_dir / '6-name2semantic.tsv'
    phoneme.write_text('voice.wav\tAA B\t[1, 1]\ttext\n', encoding='utf-8')
    semantic.write_text('voice.wav\t1 2 3\n', encoding='utf-8')
    feature_manifest = feature_dir / 'feature-manifest.json'
    feature_manifest.write_text(json.dumps({
        'profile_id':profile.id, 'dataset_snapshot_id':dataset.id,
        'snapshot_sha256':dataset.snapshot_sha256, 'list_sha256':dataset.list_sha256,
        'feature_files':{'phoneme':str(phoneme), 'semantic':str(semantic)},
    }), encoding='utf-8')
    workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.TRAINING,
                                status=WorkflowStatus.CANCELLED, dataset_snapshot_id=dataset.id,
                                snapshot_sha256=dataset.snapshot_sha256,
                                processing_options={'quality':'quick'})
    store.save_workflow(project, workflow)

    service.resume(workflow.id)

    current = store.load_workflow(project, workflow.id)
    assert current.status == WorkflowStatus.RUNNING
    assert current.attempt == 1
    assert current.training_run_id
    assert worker.sent[-1][1] == 'train'
    assert worker.sent[-1][2]['training_run_id'] == current.training_run_id
    assert worker.sent[-1][2]['sovits_epochs'] == 4
    service.close()


def test_cancelled_training_resume_rebuilds_empty_feature_cache(tmp_path):
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('重建特征', True, consent_record='user consent', consent_confirmed_at='2026-09-09')
    store.save_profile(project, profile)
    dataset_folder = project / 'datasets' / 'empty-feature-snapshot'
    dataset_folder.mkdir(parents=True)
    list_path = dataset_folder / 'dataset.list'
    list_path.write_text('', encoding='utf-8')
    dataset = DatasetManifest('frozen', profile.id, id='empty-feature-snapshot', frozen=True,
                              list_path=str(list_path), wav_dir=str(dataset_folder / 'audio'),
                              list_relative_path='datasets/empty-feature-snapshot/dataset.list',
                              list_sha256=sha256_file(list_path))
    dataset.snapshot_sha256 = dataset_snapshot_sha256(dataset)
    store.save_dataset_snapshot(project, dataset)
    feature_dir = paths.data_root / 'training' / profile.id / dataset.snapshot_sha256 / 'features'
    feature_dir.mkdir(parents=True)
    phoneme = feature_dir / '2-name2text.txt'
    semantic = feature_dir / '6-name2semantic.tsv'
    phoneme.write_text('\n', encoding='utf-8')
    semantic.write_text('\n', encoding='utf-8')
    (feature_dir / 'feature-manifest.json').write_text(json.dumps({
        'profile_id':profile.id, 'dataset_snapshot_id':dataset.id,
        'snapshot_sha256':dataset.snapshot_sha256, 'list_sha256':dataset.list_sha256,
        'feature_files':{'phoneme':str(phoneme), 'semantic':str(semantic)},
    }), encoding='utf-8')
    workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.TRAINING,
                                status=WorkflowStatus.CANCELLED, dataset_snapshot_id=dataset.id,
                                snapshot_sha256=dataset.snapshot_sha256,
                                processing_options={'quality':'quick'})
    store.save_workflow(project, workflow)

    service.resume(workflow.id)

    current = store.load_workflow(project, workflow.id)
    assert current.stage == WorkflowStage.FEATURE_PREPARING
    assert current.status == WorkflowStatus.RUNNING
    assert worker.sent[-1][1] == 'prepare_dataset'
    service.close()


def test_singing_is_chained_once_after_text_voice_finishes(tmp_path, monkeypatch):
    from local_voice_studio.models import TrainingWorkflow, WorkflowStage
    app = _app()
    paths, store, project, worker, _ = _fixture(tmp_path)
    service = TrainingService(paths, store, project, worker)
    profile = VoiceProfile('双能力', True); store.save_profile(project, profile)
    workflow = TrainingWorkflow(profile.id, profile.name, stage=WorkflowStage.SAVED,
                                processing_options={'train_singing':True})
    calls=[]
    monkeypatch.setattr(service,'train_singing',lambda profile_id:calls.append(profile_id))
    service._on_workflow(workflow)
    service._on_workflow(workflow)
    assert calls == [profile.id]
    service.close()


@pytest.mark.parametrize('fmt,codec', [('flac','flac'),('m4a','aac')])
def test_new_export_formats_encode_and_validate_real_audio(tmp_path, fmt, codec):
    from local_voice_studio.cover.exporting import FFmpegExportBackend, ExportOutputValidator
    tools = Path(os.environ.get('LOCALAPPDATA','')) / 'LocalVoiceStudio' / 'tools'
    if not (tools / 'ffmpeg.exe').is_file(): pytest.skip('local FFmpeg unavailable')
    source = _wav(tmp_path / 'source.wav', 1)
    target = tmp_path / ('中文 导出.' + fmt)
    FFmpegExportBackend(tools / 'ffmpeg.exe').encode(source, target, format=fmt)
    result = ExportOutputValidator(ffprobe=tools/'ffprobe.exe').validate(target, expected_format=fmt, source_duration_seconds=1)
    assert result.codec_name == codec
    assert result.sample_rate == 48000 and result.channels == 2
