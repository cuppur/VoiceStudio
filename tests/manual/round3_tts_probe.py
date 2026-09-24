"""Read an existing verified local model and synthesize one isolated short clip."""
from __future__ import annotations

import json
import tempfile
import time
import wave
import array
import shutil
from pathlib import Path

from local_voice_studio.engine import GptSovitsEngine
from local_voice_studio.audio import sha256_file
from local_voice_studio.paths import AppPaths
from local_voice_studio.worker import WorkerService


def main():
    paths=AppPaths.default()
    project=None; profile=None
    for manifest_path in paths.projects_root.glob('*/project.json'):
        try: manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError,ValueError): continue
        selected=next((p for p in manifest.get('voice_profiles',[]) if p.get('active_model_version_id')
                       and p.get('active_gpt_checkpoint') and p.get('active_sovits_checkpoint')
                       and any(r.get('approved') and r.get('transcript') and Path(r.get('path','')).is_file()
                               for r in p.get('reference_assets',[]))),None)
        if selected:
            project=manifest_path.parent; profile=selected; break
    if project is None or profile is None:
        raise RuntimeError('本地没有已启用的 TTS 模型和已授权参考音频')
    # Exercise the actual worker migration against an isolated manifest whose
    # checkpoint files are hard-linked read-only inputs. The user's project is
    # never modified by this probe.
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='voicestudio-worker-migration-') as root_text:
        root=Path(root_text); data_root=root/'data'; projects_root=data_root/'projects'
        test_project=projects_root/'legacy-profile'; checkpoint_root=test_project/'checkpoints'
        checkpoint_root.mkdir(parents=True)
        tested_profile=json.loads(json.dumps(profile))
        tested_profile['project_path']=str(test_project)
        tested_profile['active_model_trust_status']='legacy-pending'
        for suffix in ('gpt','sovits'):
            source=Path(profile[f'active_{suffix}_checkpoint'])
            assert sha256_file(source)==profile[f'active_{suffix}_sha256']
            destination=checkpoint_root/source.name
            try: destination.hardlink_to(source)
            except OSError: shutil.copy2(source,destination)
            tested_profile[f'active_{suffix}_checkpoint']=str(destination)
        test_manifest=test_project/'project.json'
        test_manifest.write_text(json.dumps({'voice_profiles':[tested_profile]},ensure_ascii=False),encoding='utf-8')
        test_paths=AppPaths(data_root,projects_root,paths.runtime_root,paths.engine_root,
                            data_root/'models',data_root/'logs',data_root/'studio.sqlite3',data_root/'cache')
        worker=WorkerService(test_paths,singing_engine=object())
        assert worker._upgrade_legacy_profile(tested_profile)
        saved=json.loads(test_manifest.read_text(encoding='utf-8'))['voice_profiles'][0]
        assert saved['active_model_trust_status']=='trusted-local'
        assert tested_profile['active_model_trust_status']=='trusted-local'
        profile['active_model_trust_status']='trusted-local'
        migration_seconds=round(time.monotonic()-started,2)
    ref=next(r for r in profile['reference_assets'] if r.get('approved') and r.get('transcript') and Path(r['path']).is_file())
    engine=GptSovitsEngine(paths)
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='voicestudio-tts-probe-') as root:
        engine.load(profile)
        sr,path,frames=engine.synthesize_segment({
            'text':'你好，这是声音克隆功能测试。', 'text_lang':'zh',
            'ref_audio_path':ref['path'], 'prompt_text':ref['transcript'],
            'prompt_lang':ref.get('language','en'),
        },Path(root)/'test.wav')
        with wave.open(str(path),'rb') as audio:
            assert audio.getnframes()>0 and audio.getframerate()==sr
            pcm=array.array('h'); pcm.frombytes(audio.readframes(audio.getnframes()))
            peak=max((abs(value) for value in pcm),default=0)
            rms=(sum(value*value for value in pcm)/max(1,len(pcm)))**0.5
            assert peak>0 and rms>0
            duration=audio.getnframes()/audio.getframerate()
            channels=audio.getnchannels()
        print(json.dumps({'ok':True,'seconds':round(time.monotonic()-start,2),
                          'migration_seconds':migration_seconds,'sample_rate':sr,'channels':channels,
                          'duration_seconds':round(duration,2),'peak':peak,'rms':round(rms,2),
                          'frames':frames,'bytes':path.stat().st_size},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
