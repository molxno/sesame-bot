import asyncio

from sesame.app import SesameBot
from sesame.cog import Music
from sesame.config import Settings

EXPECTED = {"play", "skip", "stop", "leave", "queue", "nowplaying", "volume", "test_audio", "tts_on", "tts_off", "help"}


def test_bot_wires_commands_safely():
    async def main():
        bot = SesameBot(Settings(token="x"))
        await bot.add_cog(Music(bot))
        assert EXPECTED <= {c.name for c in bot.commands}
        assert EXPECTED <= {c.name for c in bot.tree.get_commands()}
        assert bot.allowed_mentions.everyone is False and bot.allowed_mentions.users is False
        assert not bot.intents.members and not bot.intents.presences
        await bot.remove_cog("Music")

    asyncio.run(main())
