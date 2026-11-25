import discord

from sesame.audio import MixerSource, TtsQueue, mix_duck, tts_text
from sesame.logging_setup import RedactSecrets


class FakeSource(discord.AudioSource):
    def __init__(self, frames):
        self.frames = list(frames)
        self.cleaned = False

    def read(self):
        return self.frames.pop(0) if self.frames else b""

    def cleanup(self):
        self.cleaned = True


def test_mix_duck_pads_to_longest():
    assert len(mix_duck(b"\x10\x00" * 4, b"\x01\x00" * 2, 0.5)) == 8


def test_tts_queue_is_bounded():
    q = TtsQueue(2)
    a, b, c = FakeSource([]), FakeSource([]), FakeSource([])
    assert q.push(a) and q.push(b)
    assert not q.push(c) and c.cleaned
    assert q.is_full()
    q.clear()
    assert len(q) == 0 and a.cleaned and b.cleaned


def test_mixer_plays_tts_over_music():
    q = TtsQueue(3)
    q.push(FakeSource([b"\x01\x00" * 4]))
    mixer = MixerSource(FakeSource([b"\x10\x00" * 4, b"\x10\x00" * 4]), q, 0.5)
    assert mixer.read() != b"\x10\x00" * 4   # mezclado
    assert mixer.read() == b"\x10\x00" * 4   # TTS terminó, solo música
    assert mixer.read() == b""


def test_tts_text_hides_links_and_limits():
    text = tts_text("Ana", "mira https://evil.example/x  esto", 200)
    assert "https" not in text and text.startswith("Ana dice:")
    assert tts_text("Ana", "   ", 200) == ""


def test_redacts_token_shape():
    import logging

    # Token falso armado en tiempo de ejecución (no es un secreto real).
    token = ".".join(["A" * 26, "B" * 6, "C" * 38])
    record = logging.LogRecord("x", logging.INFO, __file__, 1, f"token={token}", (), None)
    RedactSecrets().filter(record)
    assert token not in record.getMessage()
