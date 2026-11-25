from sesame.config import Settings


def test_defaults(monkeypatch):
    for k in ("MAX_QUEUE", "ALLOWED_GUILD_IDS", "MAX_GUILDS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.max_queue == 20 and s.max_guilds == 10 and not s.allowed_guild_ids
    assert s.guild_allowed(123)


def test_bounds_and_allowlist(monkeypatch):
    monkeypatch.setenv("MAX_QUEUE", "999999")
    monkeypatch.setenv("MAX_VOICE_GUILDS", "-5")
    monkeypatch.setenv("TTS_DUCK", "nope")
    monkeypatch.setenv("ALLOWED_GUILD_IDS", "1528586575003516958, abc;42")
    s = Settings.from_env()
    assert s.max_queue == 100
    assert s.max_voice_guilds == 1
    assert s.tts_duck == 0.22
    assert s.allowed_guild_ids == {1528586575003516958, 42}
    assert not s.guild_allowed(7)
