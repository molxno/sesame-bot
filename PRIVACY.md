# Política de Privacidad de Sesame

_Última actualización: 25 de noviembre de 2026_

Esta política explica qué datos trata Sesame, el bot de Discord mantenido por **molxno** (Santiago Molano Holguín), y para qué.

## 1. Datos que procesa
- **Mensajes de texto**: Sesame lee el contenido de los mensajes de los canales que puede ver **solo para detectar comandos** (por ejemplo `!play`). Si alguien activa `tts_on` en un canal, también procesa los mensajes de ese canal escritos por personas que están en el mismo canal de voz que el bot, para leerlos en voz alta.
- **Identificadores de Discord**: IDs de servidores, canales y usuarios, y el nombre visible de quien escribe (se dice antes del mensaje leído).
- **Estado de voz**: en qué canal de voz está cada persona, para unirse al canal correcto y salir cuando queda vacío. El bot entra ensordecido y **no escucha ni graba audio** de los usuarios.
- **Búsquedas**: el texto o enlace que se pasa a `play`.

## 2. Para qué se usan
Únicamente para prestar las funciones del bot: reproducir música, leer el chat en voz alta y aplicar límites contra abuso.

## 3. Almacenamiento
Sesame **no guarda** mensajes, búsquedas ni audio en ninguna base de datos. Las colas y los límites anti-spam existen solo en memoria y se borran al reiniciar el bot. Los registros técnicos (logs) pueden incluir IDs de servidores y canales y títulos de canciones; se usan para diagnosticar errores y no se conservan de forma permanente.

## 4. Servicios de terceros
Para funcionar, Sesame envía datos a:
- **Google Translate (gTTS)**: el texto de los mensajes que se leen en voz alta, para generar la voz.
- **SoundCloud, YouTube, Bandcamp y Spotify**: las búsquedas o enlaces de `play` (en Spotify solo se consulta el nombre de la canción).
- **Microsoft Azure**: infraestructura donde se ejecuta el bot.
- **Discord**: plataforma sobre la que funciona el bot.

Cada servicio tiene su propia política de privacidad.

## 5. Compartir datos
Sesame no vende ni comparte datos con fines comerciales o publicitarios.

## 6. Tus opciones
- Desactiva la lectura del chat con `tts_off` o no escribas en el canal leído.
- Un administrador puede expulsar el bot del servidor en cualquier momento; eso detiene todo tratamiento de datos de ese servidor.
- Para consultas o solicitudes sobre tus datos (incluidos tus derechos bajo la Ley 1581 de 2012 de Colombia), abre un *issue* en https://github.com/molxno/sesame-bot/issues.

## 7. Menores
Sesame sigue las reglas de edad de Discord y no está dirigido a menores de la edad mínima que exige Discord en cada país.

## 8. Cambios
Esta política puede actualizarse. La versión vigente es la publicada en este repositorio.
