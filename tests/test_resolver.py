import pytest

from sesame.resolver import Kind, ResolveError, TrackTooLong, classify, clean_text, plan, validate_info


@pytest.mark.parametrize(
    "query,kind",
    [
        ("lo siento roberto lugo", Kind.TEXT),
        ("https://www.youtube.com/watch?v=yPpJVFenDvw", Kind.YOUTUBE),
        ("https://youtu.be/yPpJVFenDvw", Kind.YOUTUBE),
        ("<https://music.youtube.com/watch?v=abc>", Kind.YOUTUBE),
        ("https://soundcloud.com/artist/track", Kind.SOUNDCLOUD),
        ("https://on.soundcloud.com/abc123", Kind.SOUNDCLOUD),
        ("https://artist.bandcamp.com/track/song", Kind.BANDCAMP),
        ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=xyz", Kind.SPOTIFY),
        ("https://open.spotify.com/intl-es/track/4cOdK2wGLETKBW3PvgPWqT", Kind.SPOTIFY),
    ],
)
def test_classify_allowed(query, kind):
    assert classify(query)[0] is kind


def test_spotify_query_is_normalized():
    _, url = classify("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=tracking")
    assert url == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    "query",
    [
        "http://169.254.169.254/metadata/instance",       # metadatos de Azure (SSRF)
        "http://localhost:42356/msi/token",                # identidad administrada
        "http://127.0.0.1/",
        "https://soundcloud.com.evil.example/track",       # dominio parecido
        "https://evil.example/?u=https://soundcloud.com",
        "https://user:pass@soundcloud.com/x",              # credenciales embebidas
        "https://soundcloud.com:8443/x",                   # puerto raro
        "file:///etc/passwd",
        "ftp://example.com/a.mp3",
        "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://a.b.bandcamp.com/track/x",
        "https://example.com/song.mp3",
    ],
)
def test_classify_rejects(query):
    with pytest.raises(ResolveError):
        classify(query)


def test_plan_text_tries_soundcloud_then_youtube():
    attempts = plan(Kind.TEXT, "song", None)
    assert [a.source for a in attempts] == ["SoundCloud", "YouTube"]
    assert attempts[0].target == "scsearch1:song"


def test_plan_youtube_falls_back_to_title():
    attempts = plan(Kind.YOUTUBE, "https://youtu.be/x", "Artista Canción")
    assert [a.source for a in attempts] == ["YouTube", "SoundCloud"]


def test_plan_spotify_without_title_is_empty():
    assert plan(Kind.SPOTIFY, "https://open.spotify.com/track/x", None) == []


def _info(**overrides):
    info = {"title": "Song", "duration": 200, "url": "https://cdn.example/a.mp3"}
    info.update(overrides)
    return info


def test_validate_ok_and_title_cleaned():
    track = validate_info(_info(title="Song\n@everyone\x00"), "SoundCloud", 900)
    assert track.duration == 200 and track.source == "SoundCloud"
    assert "\n" not in track.title and "\x00" not in track.title


def test_validate_takes_first_entry():
    track = validate_info({"entries": [None, _info(title="A")]}, "YouTube", 900)
    assert track.title == "A"


@pytest.mark.parametrize(
    "info",
    [
        None,
        {"entries": []},
        _info(duration=None),
        _info(is_live=True),
        _info(live_status="is_live"),
        _info(url="file:///etc/passwd"),
        _info(url="concat:/a|/b"),
    ],
)
def test_validate_rejects(info):
    with pytest.raises(ResolveError):
        validate_info(info, "x", 900)


def test_validate_too_long():
    with pytest.raises(TrackTooLong):
        validate_info(_info(duration=10_000), "x", 900)


def test_validate_drops_header_injection():
    track = validate_info(_info(http_headers={"A": "ok", "B": "x\r\nEvil: 1"}), "x", 900)
    assert track.headers == {"A": "ok"}


def test_clean_text_limits():
    assert clean_text("  a \t b\n c  ", 100) == "a b c"
    assert len(clean_text("x" * 1000, 50)) == 50
