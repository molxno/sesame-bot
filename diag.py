import json, re, urllib.request, yt_dlp
qs = ["siempre sere tito rojas", "hace calor plan b", "siempre sere tony vega", "lo siento roberto lugo"]
for q in qs:
    print("=== FLAT", q)
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": "in_playlist", "ignoreerrors": True}) as y:
        info = y.extract_info(f"scsearch8:{q}", download=False)
    for e in (info or {}).get("entries") or []:
        if e: print(" ", {k: e.get(k) for k in ("title","uploader","duration","url","ie_key")})
    print("=== FIRST FULL", q)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "format":"bestaudio/best","noplaylist":True}) as y:
            i = y.extract_info(f"scsearch1:{q}", download=False)
        e = i["entries"][0]; print("  OK", e.get("title"), e.get("duration"), (e.get("url") or "")[:60])
    except Exception as ex: print("  FAIL", str(ex)[:300])
for u in ["https://open.spotify.com/track/4ZGk5h0slfeunWEKzXEoHr"]:
    for h in ({"User-Agent":"Mozilla/5.0","Accept-Language":"en"},):
        html = urllib.request.urlopen(urllib.request.Request(u, headers=h), timeout=10).read(300000).decode("utf-8","replace")
        print("TITLE", re.findall(r"<title>(.*?)</title>", html)[:1], re.findall(r'og:description" content="([^"]*)', html)[:1], re.findall(r'music:musician_description" content="([^"]*)', html)[:1])
    print("OEMBED", urllib.request.urlopen(f"https://open.spotify.com/oembed?url={u}", timeout=10).read()[:300])
try:
    with yt_dlp.YoutubeDL({"quiet": True,"format":"bestaudio/best","extractor_args":{"youtube":{"player_client":["tv","mweb","web_safari"]}}}) as y:
        i = y.extract_info("ytsearch1:siempre sere tito rojas", download=False); print("YT OK", i["entries"][0].get("title"))
except Exception as ex: print("YT FAIL", str(ex)[:200])
