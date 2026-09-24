"""Run a short, isolated real-engine audio probe without modifying user projects."""
from __future__ import annotations

import json
import math
import os
import struct
import tempfile
import time
import wave
from pathlib import Path

from local_voice_studio.paths import AppPaths
from local_voice_studio.storage import StudioStore
from local_voice_studio.cover.project import CoverProject
from local_voice_studio.cover.separation import SongSeparationPipeline


def main() -> None:
    installed = AppPaths.default()
    with tempfile.TemporaryDirectory(prefix='voicestudio-real-probe-') as root_text:
        root = Path(root_text)
        paths = AppPaths(installed.data_root,root/'projects',installed.runtime_root,
                         installed.engine_root,installed.models_root,root/'logs',
                         root/'studio.sqlite3',root/'cache')
        store = StudioStore(paths)
        project = store.create_project('真实音频隔离测试')
        source = root/'隔离测试歌曲.wav'
        with wave.open(str(source),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(32000)
            samples=[]
            for i in range(32000*8):
                t=i/32000
                v=.22*math.sin(2*math.pi*220*t)+.12*math.sin(2*math.pi*440*t)
                samples.append(struct.pack('<h',int(max(-1,min(1,v))*32767)))
            out.writeframes(b''.join(samples))
        cover=CoverProject.create(project,title='隔离测试歌曲')
        cover.copy_source(source);cover.attest_rights(True)
        stages=[]
        start=time.monotonic()
        pipeline=SongSeparationPipeline(project,paths=paths)
        result=pipeline.separate(cover.id,cover.source_relative_path,cover.source_sha256,
                                 engine_id=os.environ.get('VS_PROBE_SEPARATOR','uvr5'),
                                 progress=lambda percent,stage,message:stages.append((round(percent,2),stage,message)))
        vocals=Path(result['vocal_path']);backing=Path(result['instrumental_path'])
        for item in (vocals,backing):
            assert item.is_file() and item.stat().st_size>44
            with wave.open(str(item),'rb') as audio:
                assert audio.getnframes()>0 and audio.getframerate()>0
        print(json.dumps({'ok':True,'seconds':round(time.monotonic()-start,2),
                          'engine':result['separator'],'stages':stages,
                          'vocal_bytes':vocals.stat().st_size,'instrumental_bytes':backing.stat().st_size},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
