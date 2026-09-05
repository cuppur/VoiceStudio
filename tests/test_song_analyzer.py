from pathlib import Path
from local_voice_studio.cover.application.analyzer import SongAnalyzer
from local_voice_studio.ui.cover_session import AudioMetadata

def test_song_analyzer_uses_waveform_cache(tmp_path, monkeypatch):
    wav=tmp_path/"x.wav"; wav.write_bytes(b"audio")
    calls={"probe":0,"decode":0}
    monkeypatch.setattr("local_voice_studio.ui.cover_session.probe_audio_metadata", lambda *a,**k: calls.__setitem__("probe",calls["probe"]+1) or AudioMetadata(.1,48000,1,"pcm",1))
    monkeypatch.setattr("local_voice_studio.ui.cover_session.decode_pcm_peaks", lambda *a,**k: calls.__setitem__("decode",calls["decode"]+1) or [(0,1)])
    a=SongAnalyzer(tmp_path/"cache",peak_count=10)
    first=a.analyze(wav); second=a.analyze(wav)
    assert not first.cache_hit and second.cache_hit
    assert calls=={"probe":1,"decode":1}
