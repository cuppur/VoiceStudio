from pathlib import Path
from local_voice_studio.infrastructure.cache import CacheStore, build_cache_key


def test_cache_key_is_stable_and_parameter_sensitive():
    common = dict(source_sha256="a" * 64, operation="separation", engine_version="uvr-1")
    assert build_cache_key(**common, parameters={"stem": "vocal"}) == build_cache_key(**common, parameters={"stem": "vocal"})
    assert build_cache_key(**common, parameters={"stem": "vocal"}) != build_cache_key(**common, parameters={"stem": "instrumental"})


def test_cache_store_publishes_atomically_and_verifies(tmp_path):
    source = tmp_path / "source.wav"; source.write_bytes(b"audio")
    store = CacheStore(tmp_path / "cache")
    destination = store.path_for("waveform", "abc", ".json")
    digest = store.publish(source, destination)
    assert store.is_valid(destination, digest)
    destination.write_bytes(b"corrupt")
    assert not store.is_valid(destination, digest)
