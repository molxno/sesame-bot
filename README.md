<p align="center">
  <img src="assets/sesame-avatar.jpg" width="160" height="160" alt="Sesame">
</p>

<h1 align="center">Sesame</h1>

<p align="center">
  <strong>Bot de Discord de música y voz.</strong><br>
  Entra al canal, pone canciones y lee el chat en voz alta.<br>
  Mientras habla, baja la música — no la para.
</p>

<p align="center">
  <a href="https://discord.com/oauth2/authorize?client_id=1575009817263874148&permissions=2181656960&scope=bot%20applications.commands"><img src="https://img.shields.io/badge/Invitar%20a%20Sesame-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Invitar a Sesame"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/Licencia-MIT-C4A35A?style=for-the-badge" alt="MIT"></a>
</p>

<p align="center">
  <img src="assets/sesame-banner.jpg" width="100%" alt="Sesame — música y chat en voz">
</p>

## Qué es

Sesame es un bot **público y open source**. Cualquier persona puede añadirlo a su servidor. No hace falta instalar nada: pulsa **Invitar a Sesame**, elige el servidor y usa `/play` o `!help`.

- Reproduce audio: busca por nombre (SoundCloud y, si falla, YouTube) o acepta enlaces de SoundCloud, YouTube, Spotify (canciones) y Bandcamp.
- Lee el chat en voz alta (`!tts_on`) y **atenúa** la canción mientras habla.
- Cola, volumen, skip y auto-salida si el canal se queda vacío.
- Comandos con `!` y con `/`.

**Invitación (cualquier servidor):**  
https://discord.com/oauth2/authorize?client_id=1575009817263874148&permissions=2181656960&scope=bot%20applications.commands

Quien invite necesita permiso de **Gestionar servidor**. Intents en el portal: Message Content, y el bot marcado como **Public Bot**.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `!help` / `/help` | Lista todos los comandos |
| `!play <canción o enlace>` / `/play` | Encola y reproduce |
| `!skip` / `/skip` (`!next`, `!siguiente`) | Siguiente de la cola |
| `!stop` / `/stop` | Para todo y vacía la cola |
| `!queue` / `/queue` | Qué suena y qué sigue |
| `!nowplaying` / `/nowplaying` | Pista actual y volumen |
| `!volume <0-100>` / `/volume` | Volumen |
| `!leave` / `/leave` | Sale del canal de voz |
| `!tts_on` / `/tts_on` | Lee este canal (baja la música) |
| `!tts_off` / `/tts_off` | Deja de leer el chat |
| `!test_audio` / `/test_audio` | Tono de 3 s (solo moderadores) |

`skip`, `stop`, `volume`, `leave` y `tts_on/off` solo funcionan si estás en el mismo canal de voz que el bot (los moderadores pueden siempre).

## Autohospedaje

Hace falta Python 3.12+, [FFmpeg](https://ffmpeg.org/) y un token de bot. Copia `.env.example` a `.env` y pon `DISCORD_TOKEN` (nunca lo subas a git).

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
python bot.py
```

En Windows también puedes usar `run_bot.bat`.

### Azure for Students (recomendado, 0 USD en tarjeta)

Suscripción **Azure for Students**: 100 USD de crédito / 12 meses, **sin tarjeta**, renovable mientras estudies. Tiene **límite de gasto**: si el crédito se acaba, los recursos se paran; no te facturan.

Con Container Apps (0.25 vCPU, 0.5 GiB, 1 réplica siempre encendida) el crédito suele durar **unos 6–12 meses** de Sesame 24/7, según cuánto suene música (eso pasa el CPU a tarifa “activa”). Luego renuevas el pack de estudiante y vuelves a tener 100 USD.

No actives Application Insights, no crees VMs extra, no pases a *pay-as-you-go*.

### Docker (24/7)

```bash
docker build -t sesame-bot .
docker run --env-file .env sesame-bot
```

### Railway

`railway.json` deja **1 réplica**, tope **0.12 vCPU** y **256 MB**. Al máximo 24/7 eso ronda **5 USD** de uso (RAM ~2.56 + CPU ~2.40). No generes dominio.

- **Free** (0 USD, sin tarjeta): 1 USD de uso al mes. Si se acaba, el bot se apaga; no hay cobro extra.
- **Hobby** (5 USD/mes en tarjeta): incluye 5 USD de uso. Con estos topes no debería haber extras. En **Workspace → Usage** pon alerta a **5 USD** y hard limit al mínimo de Railway (suele ser 10 USD).

Pasos: New project → GitHub `sesame-bot` → variable `DISCORD_TOKEN` → 1 réplica. Cuando esté `Conectado como Sesame`, apaga el bot de tu PC.

## Seguridad y control de costos

- **Enlaces en lista blanca**: solo SoundCloud, YouTube, Spotify (canciones) y Bandcamp. Cualquier otra URL se rechaza antes de tocar la red (evita SSRF hacia `localhost` o los metadatos de Azure). FFmpeg solo puede abrir protocolos de red.
- **Sin menciones**: el bot nunca hace `@everyone` ni menciona a nadie, aunque el título de una canción lo intente.
- **Anti-spam**: límites por usuario y por servidor para comandos, `play` y TTS; los avisos de "vas muy rápido" también están limitados para que el bot no se use para hacer spam.
- **Trabajo pesado acotado**: búsquedas y TTS corren en grupos de hilos fijos con timeout; si se llenan, el bot responde "ocupado".
- **Topes de consumo**: 3 servidores con voz a la vez, cola de 20, canciones de 15 min, salida por inactividad (5 min) o canal vacío (15 s), sin directos. El bot entra **ensordecido** (no recibe el audio de los demás: menos tráfico y menos costo en Azure).
- **Servidores**: como máximo `MAX_GUILDS` (10). Con `ALLOWED_GUILD_IDS` puedes limitarlo a tus servidores; en cualquier otro se sale solo.
- **Contenedor sin root**, token solo como secret de Azure y nunca en logs (se enmascara).

Todos los límites se cambian con variables de entorno (ver `sesame/config.py` y `azure-containerapp.yaml`).

## Actualizar en Azure

Cada push a `main` pasa los tests y publica `ghcr.io/molxno/sesame-bot:v<número>` (el número de la ejecución en **Actions**). En Azure: **Containers → Properties → Image and tag** = `molxno/sesame-bot:v<número>` → **Save as a new revision**. En el Log stream verás `versión=v<número>`.

## Desarrollo

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Estructura: `sesame/config.py` (límites), `resolver.py` (fuentes y lista blanca), `audio.py` (FFmpeg, TTS, mezclador), `player.py` (cola por servidor), `cog.py` (comandos), `guards.py` (anti-spam), `dave.py` (cifrado de voz), `app.py` (bot).

## Aviso

discord.py 2.7.1 no llama las transiciones de davey 0.2.0 (cifrado de voz DAVE). `sesame/dave.py` incluye un puente para eso.

## Legal

- [Condiciones del Servicio](TERMS.md)
- [Política de Privacidad](PRIVACY.md)

## Licencia

[MIT](LICENSE) © 2026 [molxno](https://github.com/molxno)
