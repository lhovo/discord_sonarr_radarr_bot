import unittest
from unittest.mock import AsyncMock, patch

import discord_media


class FakeCtx:
    def __init__(self):
        self.channel = type("Channel", (), {"id": 1})()
        self.messages: list[str] = []

    async def send(self, content: str):
        self.messages.append(content)


class TestTvCommandDispatch(unittest.IsolatedAsyncioTestCase):
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

            lookup_mock.assert_awaited_once_with(ctx, "the office")
            queue_mock.assert_not_awaited()
            show_mock.assert_not_awaited()

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
