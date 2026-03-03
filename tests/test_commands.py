import unittest
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["DISCORD_MEDIA_CONFIG_PATH"] = str(Path(__file__).with_name("test_config.yaml"))
import discord_media


class FakeCtx:
    def __init__(self):
        self.channel = type("Channel", (), {"id": 1})()
        self.clean_prefix = "!"
        self.messages: list[str] = []
        self.embeds = []

    async def send(self, content: str | None = None, *, embed=None, embeds=None):
        if content is not None:
            self.messages.append(content)
        if embed is not None:
            self.embeds.append(embed)
        if embeds is not None:
            self.embeds.extend(embeds)


class TestTvCommandDispatch(unittest.IsolatedAsyncioTestCase):
    async def test_help_command_sends_formatted_embed(self):
        ctx = FakeCtx()

        await discord_media.help_command.callback(ctx)

        self.assertEqual(len(ctx.embeds), 1)
        emb = ctx.embeds[0]
        self.assertIn("Commands", emb.title)
        self.assertIn("tv ", emb.fields[0].value)
        self.assertIn("tvadd", emb.fields[0].value)
        self.assertIn("tvsearch", emb.fields[0].value)
        self.assertEqual(emb.fields[2].name, "Other")
        self.assertIn("help", emb.fields[2].value)

    async def test_tv_command_no_arg_calls_download_queue(self):
        ctx = FakeCtx()
        with (
            patch.object(discord_media.sonarr, "tv_download_queue", new=AsyncMock()) as queue_mock,
            patch.object(discord_media.sonarr, "tv_show", new=AsyncMock()) as show_mock,
            patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock,
        ):
            await discord_media.tv_command.callback(ctx, arg=None)

            queue_mock.assert_awaited_once_with(ctx)
            show_mock.assert_not_awaited()
            lookup_mock.assert_not_awaited()

    async def test_tv_command_numeric_arg_calls_tv_show(self):
        ctx = FakeCtx()
        with (
            patch.object(discord_media.sonarr, "tv_download_queue", new=AsyncMock()) as queue_mock,
            patch.object(discord_media.sonarr, "tv_show", new=AsyncMock()) as show_mock,
            patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock,
        ):
            await discord_media.tv_command.callback(ctx, arg="12345")

            show_mock.assert_awaited_once_with(ctx, 12345)
            queue_mock.assert_not_awaited()
            lookup_mock.assert_not_awaited()

    async def test_tv_command_text_arg_calls_tv_lookup(self):
        ctx = FakeCtx()
        with (
            patch.object(discord_media.sonarr, "tv_download_queue", new=AsyncMock()) as queue_mock,
            patch.object(discord_media.sonarr, "tv_show", new=AsyncMock()) as show_mock,
            patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock,
        ):
            await discord_media.tv_command.callback(ctx, arg="the office")

            lookup_mock.assert_awaited_once_with(ctx, "the office", limit=5)
            queue_mock.assert_not_awaited()
            show_mock.assert_not_awaited()

    async def test_tv_command_text_arg_with_inline_limit_calls_tv_lookup(self):
        ctx = FakeCtx()
        with (
            patch.object(discord_media.sonarr, "tv_download_queue", new=AsyncMock()) as queue_mock,
            patch.object(discord_media.sonarr, "tv_show", new=AsyncMock()) as show_mock,
            patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock,
        ):
            await discord_media.tv_command.callback(ctx, arg="the office --15")

            lookup_mock.assert_awaited_once_with(ctx, "the office", limit=15)
            queue_mock.assert_not_awaited()
            show_mock.assert_not_awaited()

    async def test_tv_command_text_arg_with_long_limit_flag_calls_tv_lookup(self):
        ctx = FakeCtx()
        with patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock:
            await discord_media.tv_command.callback(ctx, arg="the office --limit 20")

            lookup_mock.assert_awaited_once_with(ctx, "the office", limit=20)

    async def test_tv_command_with_only_limit_flag_sends_usage(self):
        ctx = FakeCtx()
        with patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock:
            await discord_media.tv_command.callback(ctx, arg="--limit 10")

            self.assertEqual(len(ctx.messages), 1)
            self.assertIn("!tv <query> [--N|--limit N]", ctx.messages[0])
            lookup_mock.assert_not_awaited()

    async def test_tv_command_invalid_limit_range_sends_error(self):
        ctx = FakeCtx()
        with patch.object(discord_media.sonarr, "tv_lookup", new=AsyncMock()) as lookup_mock:
            await discord_media.tv_command.callback(ctx, arg="the office --21")

            self.assertEqual(len(ctx.messages), 1)
            self.assertIn("between 1 and 20", ctx.messages[0])
            lookup_mock.assert_not_awaited()

    async def test_tv_search_command_valid_ref_dispatches_to_sonarr(self):
        ctx = FakeCtx()
        with patch.object(discord_media.sonarr, "search_episode", new=AsyncMock()) as search_mock:
            await discord_media.tv_search_command.callback(ctx, 12345, "s2e8")
            search_mock.assert_awaited_once_with(ctx, 12345, 2, 8)

    async def test_tv_search_command_invalid_ref_sends_usage(self):
        ctx = FakeCtx()
        with patch.object(discord_media.sonarr, "search_episode", new=AsyncMock()) as search_mock:
            await discord_media.tv_search_command.callback(ctx, 12345, "season2episode8")
            self.assertEqual(len(ctx.messages), 1)
            self.assertIn("!tvsearch", ctx.messages[0])
            search_mock.assert_not_awaited()
