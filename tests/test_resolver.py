import pytest

from sesame.resolver import (
    Kind,
    ResolveError,
    TrackTooLong,
    classify,
    clean_text,
    match_score,
    parse_spotify_page,
    plan,
    rank_candidates,
    search_terms,
    validate_info,
)


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
    assert attempts[0].search and attempts[0].target == "song"


def test_plan_youtube_link_searches_title_first():
    attempts = plan(Kind.YOUTUBE, "https://youtu.be/x", "Artista Canción")
    assert [a.source for a in attempts] == ["SoundCloud", "YouTube"]
    assert not attempts[1].search


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


# --- Búsqueda: casos reales vistos en SoundCloud ---
REAL_RESULTS = [  # "siempre sere tito rojas": oficial con DRM, adelantos de 30 s y una subida válida
    {"title": "Siempre Seré", "uploader": "Tito Rojas", "duration": 322.5, "url": "u-oficial-drm"},
    {"title": "Siempre Seré", "uploader": "Tito Rojas", "duration": 30.0, "url": "u-preview"},
    {"title": "Siempre Sere - Tito Rojas", "uploader": "Andrés", "duration": 234.4, "url": "u-fan"},
    {"title": "Siempre Seré (Legacy Mix)", "uploader": "Tito Rojas", "duration": 30.0, "url": "u-preview2"},
    {"title": "Por Siempre tuyo Sere", "uploader": "tony_ruiz64", "duration": 207.7, "url": "u-otra"},
    None,
]


def test_rank_skips_previews_and_keeps_good_matches_in_order():
    ranked = [e["url"] for e in rank_candidates("siempre sere tito rojas", REAL_RESULTS, 900)]
    assert ranked == ["u-oficial-drm", "u-fan"]


def test_rank_drops_too_long_and_unrelated():
    entries = [
        {"title": "Hace Calor", "uploader": "Plan B", "duration": 7200, "url": "largo"},
        {"title": "Buscando calor", "uploader": "Otro", "duration": 165, "url": "otra"},
        {"title": "Plan B Ft Julio Voltio - Hace Calor", "uploader": "x", "duration": 207, "url": "ok"},
    ]
    assert [e["url"] for e in rank_candidates("hace calor plan b", entries, 900)] == ["ok"]


def test_match_score_penalizes_unwanted_versions():
    assert match_score("lo siento roberto lugo", "Lo Siento", "Roberto Lugo") == 1.0
    assert match_score("lo siento roberto lugo", "Lo Siento (karaoke)", "Roberto Lugo") < 1.0
    assert match_score("lo siento karaoke", "Lo Siento karaoke", "") == 1.0
    assert match_score("siempre sere", "Siempre Seré", "") == 1.0  # tildes


def test_search_terms_cleans_youtube_titles():
    assert search_terms("Tito Rojas - Siempre Seré (Video Oficial) [HD]") == "Tito Rojas Siempre Seré"


def test_parse_spotify_page():
    page = (
        '<meta property="og:title" content="Siempre Sere"/>'
        '<meta property="og:description" content="Tito Rojas · Sensual · Song · 1990"/>'
    )
    assert parse_spotify_page(page) == "Siempre Sere Tito Rojas"
    assert parse_spotify_page("<html></html>") is None
