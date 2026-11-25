import re, urllib.request, yt_dlp, json
for tid in ["4ZGk5h0sIfeunWEKzXEoHr", "4ZGk5h0slfeunWEKzXEoHr", "4cOdK2wGLETKBW3PvgPWqT"]:
    u = f"https://open.spotify.com/track/{tid}"
    for name, url in (("PAGE", u), ("EMBED", f"https://open.spotify.com/embed/track/{tid}"), ("OEMBED", f"https://open.spotify.com/oembed?url={u}")):
        try:
            html = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Accept-Language":"en"}), timeout=10).read(400000).decode("utf-8","replace")
            print(name, tid, re.findall(r"<title>(.*?)</title>", html)[:1], re.findall(r'og:description" content="([^"]*)', html)[:1], re.findall(r'"artists":\[\{"name":"([^"]+)"', html)[:1], html[:200] if name=="OEMBED" else "")
        except Exception as ex: print(name, tid, "FAIL", ex)
try:
    with yt_dlp.YoutubeDL({"quiet": True,"format":"bestaudio/best","extractor_args":{"youtube":{"player_client":["tv","mweb","web_safari"]}}}) as y:
        i = y.extract_info("ytsearch1:siempre sere tito rojas", download=False); print("YT OK", i["entries"][0].get("title"))
except Exception as ex: print("YT FAIL", str(ex)[:200])
