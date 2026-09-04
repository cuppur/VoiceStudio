import hashlib

from local_voice_studio.models import VoiceProfile
from local_voice_studio.singing.models import RVCInferenceSettings, SingingModelVersion


def test_autotune_presets_map_to_f0_smoothing_without_overriding_manual_radius():
    assert RVCInferenceSettings.from_payload({"autotune": "off"}).filter_radius == 0
    assert RVCInferenceSettings.from_payload({"autotune": "light"}).filter_radius == 3
    assert RVCInferenceSettings.from_payload({"autotune": "medium"}).filter_radius == 7
    assert RVCInferenceSettings.from_payload({"autotune": "medium", "filter_radius": 2}).filter_radius == 2


def _model(tmp_path, profile_id="profile"):
    checkpoint = tmp_path / "models" / "model.pth"
    index = tmp_path / "models" / "model.index"
    checkpoint.parent.mkdir(exist_ok=True)
    checkpoint.write_bytes(b"checkpoint")
    index.write_bytes(b"index")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    return SingingModelVersion(
        profile_id=profile_id,
        engine="rvc_v2",
        engine_version="2.0",
        checkpoint_relative_path="models/model.pth",
        checkpoint_sha256=digest(checkpoint),
        index_relative_path="models/model.index",
        index_sha256=digest(index),
        trust_status="verified",
    )


def test_singing_model_round_trip_and_profile_round_trip(tmp_path):
    profile = VoiceProfile(name="A", consent_confirmed=True)
    model = _model(tmp_path, profile.id)
    profile.singing_models.append(model)
    profile.active_singing_model_id = model.id

    restored = VoiceProfile.from_dict(profile.to_dict())
    assert restored.singing_models[0].to_dict() == model.to_dict()
    assert restored.singing_status(tmp_path) == "ready"


def test_old_profile_payload_migrates_without_singing_fields():
    profile = VoiceProfile.from_dict({"name": "legacy", "consent_confirmed": True})
    assert profile.singing_models == []
    assert profile.active_singing_model_id == ""
    assert profile.singing_status() == "not_ready"


def test_new_singing_model_is_unverified_by_default():
    assert SingingModelVersion().trust_status == "unverified"


def test_singing_status_training_missing_and_untrusted(tmp_path):
    profile = VoiceProfile(name="A", consent_confirmed=True)
    assert profile.singing_status() == "not_ready"
    model = _model(tmp_path, profile.id)
    profile.singing_models = [model]
    profile.active_singing_model_id = model.id
    model.checkpoint_relative_path = "gone.pth"
    assert profile.singing_status(tmp_path) == "model_missing"
    model.checkpoint_relative_path = "models/model.pth"
    model.checkpoint_sha256 = "0" * 64
    assert profile.singing_status(tmp_path) == "untrusted"
    model.checkpoint_sha256 = hashlib.sha256(b"checkpoint").hexdigest()
    model.trust_status = "imported-unverified"
    assert profile.singing_status(tmp_path) == "untrusted"
    model.trust_status = "verified"
    profile.training_state = "training"
    assert profile.singing_status(tmp_path) == "training"


def test_model_belongs_to_profile_is_data_visible(tmp_path):
    profile = VoiceProfile(name="A", consent_confirmed=True)
    model = _model(tmp_path, "other-profile")
    profile.singing_models = [model]
    profile.active_singing_model_id = model.id
    # Ownership enforcement belongs to the conversion service; status remains
    # honest about the selected local model's file/trust state.
    assert profile.singing_status(tmp_path) == "untrusted"


def test_model_paths_are_project_relative(tmp_path):
    profile = VoiceProfile(name="A", consent_confirmed=True)
    model = _model(tmp_path, profile.id)
    model.checkpoint_relative_path = "../outside.pth"
    profile.singing_models = [model]
    profile.active_singing_model_id = model.id
    assert model.files_available(tmp_path) is False
    assert profile.singing_status(tmp_path) == "model_missing"


def test_active_model_switching_and_versioning(tmp_path):
    profile = VoiceProfile(name="A", consent_confirmed=True)
    first = _model(tmp_path, profile.id)
    second = _model(tmp_path, profile.id)
    # Keep versions distinct as a real training run would.
    second.id = "second-version"
    profile.singing_models = [first, second]
    profile.active_singing_model_id = first.id
    assert profile.singing_status(tmp_path) == "ready"
    profile.active_singing_model_id = second.id
    assert profile.singing_status(tmp_path) == "ready"
