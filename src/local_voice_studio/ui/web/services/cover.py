"""AI cover workflow service (import → rights → separate → convert → mix → export)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ....cover.application.service import CoverApplicationService
from ....cover.mixing.models import CoverMixSettings, GainScale
from ....cover.project import RIGHTS_ATTESTATION_TEXT, CoverProject
from ....cover.separation import RoFormerRuntimeStatus, UVR5RuntimeStatus
from .base import WebService
from ...cover_session import probe_audio_metadata, parse_lrc, write_lrc

AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")
SEPARATION_MODES = ("uvr5", "roformer")


class CoverService(WebService):
    """Real cover orchestration, free of Qt widgets."""

    # ------------------------------------------------------------------ state
    def _service(self) -> CoverApplicationService:
        return CoverApplicationService(self.project, paths=self.paths, store=self.store)

    def _cover(self, cover_id: str) -> CoverProject:
        return CoverProject.load(self.project, str(cover_id))

    def engines(self) -> dict[str, Any]:
        uvr5 = UVR5RuntimeStatus.detect(self.paths)
        roformer = RoFormerRuntimeStatus.detect(self.paths)
        return {
            "uvr5": {"ready": bool(uvr5.ready), "detail": str(getattr(uvr5, "message", "") or "")},
            "roformer": {"ready": bool(roformer.ready), "detail": str(getattr(roformer, "message", "") or "")},
        }

    def state(self, cover_id: str) -> dict[str, Any]:
        cover = self._cover(cover_id)
        assets = {str(asset.role): asset for asset in cover.assets}
        return {
            "cover_id": cover.id,
            "title": cover.title,
            "rights_confirmed": bool(cover.rights_confirmed),
            "rights_text": RIGHTS_ATTESTATION_TEXT,
            "separation_status": str(cover.separation_status),
            "ai_vocal_status": str(cover.ai_vocal_status),
            "mix_status": str(cover.mix_status),
            "export_status": str(cover.export_status),
            "has_vocal": "vocal" in assets,
            "has_instrumental": "instrumental" in assets,
            "has_ai_vocal": "ai_vocal" in assets,
            "has_final_mix": "final_mix" in assets,
            "final_asset_id": assets["final_mix"].id if "final_mix" in assets else "",
            "engines": self.engines(),
            "active_request": self.active_request(cover.id),
        }

    # ---------------------------------------------------------------- actions
    def import_song(self, source: Path) -> dict[str, Any]:
        source = Path(source)
        if not source.is_file():
            raise ValueError(f"文件不存在：{source}")
        if source.suffix.lower() not in AUDIO_SUFFIXES:
            raise ValueError(f"不支持的音频格式：{source.suffix}")
        metadata = probe_audio_metadata(source, paths=self.paths)
        if metadata.duration_seconds <= 0 or metadata.sample_rate <= 0:
            raise ValueError('文件中没有可解码的音频')
        cover = CoverProject.create(self.project, title=source.stem or "未命名翻唱")
        cover.copy_source(source)
        cover.duration_ms = round(metadata.duration_seconds * 1000)
        lrc = source.with_suffix('.lrc')
        if lrc.is_file():
            try:
                self.import_lrc(cover.id, lrc)
                cover = self._cover(cover.id)
                cover.duration_ms = round(metadata.duration_seconds * 1000)
            except (OSError, UnicodeError, ValueError):
                pass  # An invalid optional sidecar must not reject valid audio.
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return {"cover_id": cover.id, "title": cover.title}

    def import_lrc(self, cover_id: str, source: Path) -> dict[str, Any]:
        cover = self._cover(cover_id)
        if source.suffix.lower() != '.lrc' or source.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('请选择不超过 2 MB 的 LRC 歌词文件')
        try:
            text = source.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            text = source.read_text(encoding='gb18030')
        lines = parse_lrc(text)
        if not lines: raise ValueError('LRC 中没有有效的时间标签和歌词')
        target = cover.root / 'lyrics' / 'manual.lrc'
        write_lrc(target, lines)
        cover.lyrics_path = target.relative_to(cover.root).as_posix()
        cover.lyrics_origin = 'manual'
        cover.save()
        self.notify('songs.changed', {'cover_id': cover.id})
        return {'line_count': len(lines)}

    def attest_rights(self, cover_id: str, confirmed: bool = True) -> dict[str, Any]:
        cover = self._cover(cover_id)
        cover.attest_rights(bool(confirmed))
        cover.save()
        self.notify("songs.changed", {"cover_id": cover.id})
        return self.state(cover.id)

    def separate(self, cover_id: str, mode: str = "uvr5") -> dict[str, Any]:
        if str(mode) not in SEPARATION_MODES:
            raise ValueError("不支持的分离方式（当前仅 UVR5 与 RoFormer）")
        # Rights and source integrity are checked before any engine probing.
        command = self._service().prepare_separation(cover_id, mode=str(mode))
        engines = self.engines()
        if not engines.get(str(mode), {}).get("ready"):
            raise RuntimeError(f"{str(mode).upper()} 分离引擎未安装或未就绪")
        return self.start_task(
            "separate_song", command.to_worker_payload(),
            kind="separate", stage="separation", title=self._cover(cover_id).title,
            cover_id=cover_id,
        )

    def cleanup_vocal(self, cover_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"mode": "denoise", **(settings or {})}
        command = self._service().prepare_vocal_cleanup(cover_id, payload)
        return self.start_task(
            "cleanup_vocal", command.to_worker_payload(),
            kind="cleanup", stage="post_process", title=self._cover(cover_id).title,
            cover_id=cover_id,
        )

    def suggest_transpose(self, cover_id: str, profile_id: str) -> dict[str, Any]:
        command = self._service().prepare_transpose_suggestion(cover_id, profile_id)
        return self.start_task(
            "suggest_transpose", command.to_worker_payload(),
            kind="transpose", stage="analysis", title=self._cover(cover_id).title,
            cover_id=cover_id, profile_id=profile_id,
        )

    def convert_vocal(self, cover_id: str, profile_id: str, pitch_shift: int = 0,
                      settings: dict[str, Any] | None = None, cleanup: dict[str, Any] | None = None) -> dict[str, Any]:
        command = self._service().prepare_ai_vocal(
            cover_id, profile_id, pitch_shift=int(pitch_shift), inference_settings=dict(settings or {}),
        )
        payload = command.to_worker_payload()
        cover_id = str(cover_id)
        if cleanup:
            # Optional vocal cleanup first; the AI vocal command runs after it.
            cleanup_command = self._service().prepare_vocal_cleanup(cover_id, dict(cleanup))
            task = self.start_task(
                "cleanup_vocal", cleanup_command.to_worker_payload(),
                kind="cleanup", stage="post_process", title=self._cover(cover_id).title,
                cover_id=cover_id, profile_id=profile_id,
            )
            self._tasks[task["request_id"]]["on_result"] = (
                lambda _payload, payload=payload, cover_id=cover_id, profile_id=profile_id:
                self._start_convert(payload, cover_id, profile_id)
            )
            return task
        return self._start_convert(payload, cover_id, profile_id)

    def _start_convert(self, payload: dict[str, Any], cover_id: str, profile_id: str) -> dict[str, Any]:
        return self.start_task(
            "convert_vocal", payload, kind="convert", stage="voice_conversion",
            title=self._cover(cover_id).title, cover_id=cover_id, profile_id=profile_id,
        )

    def render(self, cover_id: str, profile_id: str, mix: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.mix_settings(mix or {})
        command = self._service().prepare_render(cover_id, profile_id, settings)
        return self.start_task(
            "render_cover", command.to_worker_payload(), kind="render", stage="mix",
            title=self._cover(cover_id).title, cover_id=cover_id, profile_id=profile_id,
        )

    def export(self, cover_id: str, *, format: str, file_name: str, destination: Path,
               existing_policy: str = "reject") -> dict[str, Any]:
        state = self.state(cover_id)
        command = self._service().prepare_export(
            cover_id, final_asset_id=state["final_asset_id"], format=str(format),
            file_name=str(file_name), destination=Path(destination),
            existing_policy=str(existing_policy), publication_rights_acknowledged=True,
        )
        return self.start_task(
            "export_cover", command.to_worker_payload(), kind="export", stage="export",
            title=self._cover(cover_id).title, cover_id=cover_id,
        )

    def transcribe_lyrics(self, cover_id: str, language: str = "zh") -> dict[str, Any]:
        command = self._service().prepare_lyrics_transcription(cover_id, language=str(language))
        return self.start_task(
            "transcribe_lyrics", command.to_worker_payload(), kind="lyrics", stage="lyrics",
            title=self._cover(cover_id).title, cover_id=cover_id,
        )

    def cancel(self, request_id: str = "") -> dict[str, Any]:
        return self.cancel_task(request_id)

    # ------------------------------------------------------------------ mix
    @staticmethod
    def mix_settings(mix: dict[str, Any]) -> CoverMixSettings:
        """Translate prototype slider values (0-100) into canonical dB settings."""
        def db(key: str, default_slider: float) -> float:
            value = mix.get(key)
            slider = default_slider if value is None else float(value)
            return GainScale.slider_to_db(slider)

        return CoverMixSettings(
            ai_gain_db=db("ai", 80.0),
            instrumental_gain_db=db("instrumental", 80.0),
            original_vocal_gain_db=db("original", 0.0),
            master_gain_db=float(mix.get("master_db", 0.0) or 0.0),
            normalize=bool(mix.get("normalize", True)),
            limiter=bool(mix.get("limiter", True)),
            fade_in_ms=int(mix.get("fade_in_ms", 0) or 0),
            fade_out_ms=int(mix.get("fade_out_ms", 0) or 0),
        )
