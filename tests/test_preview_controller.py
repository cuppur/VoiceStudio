from local_voice_studio.cover.preview import PlaybackMode, PreviewMixPlanner, PreviewTrack, TrackRole
from local_voice_studio.ui.audio.preview_controller import PlayerChannel, PreviewAudioController


class FakePlayer:
    def __init__(self): self.source = ""; self.pos = 0; self.events = []
    def set_source(self, path): self.source = path; self.events.append(("source", path))
    def set_position(self, pos): self.pos = pos; self.events.append(("seek", pos))
    def position(self): return self.pos
    def play(self): self.events.append("play")
    def pause(self): self.events.append("pause")
    def stop(self): self.events.append("stop")


def controller():
    return PreviewAudioController({
        role: PlayerChannel(FakePlayer()) for role in TrackRole
    }, drift_tolerance_ms=50)


def test_planner_maps_semantic_roles_not_array_indexes():
    planner = PreviewMixPlanner({TrackRole.VOCAL: "/vocal.wav", TrackRole.INSTRUMENTAL: "/inst.wav"})
    plan = planner.plan("vocal")
    assert plan.selected_role is TrackRole.VOCAL
    assert plan.source.path == "/vocal.wav"
    assert planner.plan(TrackRole.INSTRUMENTAL).source.path == "/inst.wav"


def test_final_mix_mode_selects_final_mix_role():
    plan = PreviewMixPlanner({TrackRole.FINAL_MIX: "/final.wav"}).plan(TrackRole.VOCAL, PlaybackMode.FINAL_MIX)
    assert plan.selected_role is TrackRole.FINAL_MIX
    assert plan.source.path == "/final.wav"


def test_controller_uses_selected_channel_and_shared_clock():
    ctl = controller()
    ctl.load(PreviewMixPlanner({TrackRole.VOCAL: "/vocal.wav", TrackRole.INSTRUMENTAL: "/inst.wav"}).plan(TrackRole.VOCAL))
    ctl.seek(1200); ctl.play()
    assert ctl.channels[TrackRole.VOCAL].player.source == "/vocal.wav"
    assert ctl.channels[TrackRole.VOCAL].player.pos == 1200
    assert "play" in ctl.channels[TrackRole.VOCAL].player.events
    assert "pause" in ctl.channels[TrackRole.INSTRUMENTAL].player.events


def test_resync_and_cancel_stop_players():
    ctl = controller()
    ctl.load(PreviewMixPlanner({TrackRole.VOCAL: "/vocal.wav"}).plan(TrackRole.VOCAL))
    player = ctl.channels[TrackRole.VOCAL].player
    ctl.seek(1000); player.pos = 1100
    assert ctl.resync() == 100
    assert player.pos == 1000
    ctl.cancel()
    assert ctl.cancelled and not ctl.playing
    assert "stop" in player.events
