"""Real local song analysis and waveform-cache service."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from ...ui.cover_session import SongSession

@dataclass(frozen=True)
class SongAnalysisResult:
    session: SongSession
    cache_hit: bool

class SongAnalyzer:
    def __init__(self, cache_dir: Path, *, peak_count: int = 6000):
        self.cache_dir=Path(cache_dir); self.peak_count=max(1,int(peak_count))
    def analyze(self, audio_path: Path, *, lrc_path: Path|None=None, cancel: Callable[[],bool]|None=None) -> SongAnalysisResult:
        audio_path=Path(audio_path)
        before={path.name for path in self._cache_candidates(audio_path) if path.is_file()}
        session=SongSession.load(audio_path,lrc_path=lrc_path,cache_dir=self.cache_dir,peak_count=self.peak_count,cancel=cancel)
        return SongAnalysisResult(session, bool(before))
    def _cache_candidates(self, audio_path: Path) -> list[Path]:
        # SongSession owns the versioned key; this check only distinguishes reuse from creation.
        import hashlib
        digest=hashlib.sha256(audio_path.read_bytes()).hexdigest()
        return [self.cache_dir/f"{digest}.json"] + list(self.cache_dir.glob("*.json"))
