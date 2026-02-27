import unittest
from typing import Any

from media.sonarr import SonarrClient
from media.util import WebRuntimeError


class FakeCtx:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, content: str):
        self.messages.append(content)


class SpySonarrClient(SonarrClient):
    def __init__(self):
        super().__init__({}, "sonarr")
        self.get_episodes_called = False

    async def get_series(self, tvdb_id: int) -> dict[str, Any] | None:
        return {"id": 99, "title": "My Show", "tvdbId": tvdb_id}

    async def get_episodes(self, series_id: int) -> list[dict[str, Any]] | WebRuntimeError:
        self.get_episodes_called = True
        return [
            {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True},
            {"seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
        ]

    async def queue(self, page_size: int = 500) -> list[dict[str, Any]] | WebRuntimeError:
        return [{"episodeId": 1001, "size": 100.0, "sizeleft": 25.0}]

    async def get_episode(self, episode_id: int) -> dict[str, Any] | WebRuntimeError:
        return {
            "seriesId": 99,
            "seasonNumber": 1,
            "episodeNumber": 2,
            "title": "Next Episode",
        }


class TestSonarrTvShow(unittest.IsolatedAsyncioTestCase):
    async def test_tv_show_uses_get_episodes_and_sends_summary(self):
        client = SpySonarrClient()
        ctx = FakeCtx()

        await client.tv_show(ctx, 12345)

        self.assertTrue(client.get_episodes_called)
        self.assertEqual(len(ctx.messages), 1)
        msg = ctx.messages[0]
        self.assertIn("My Show", msg)
        self.assertIn("Season 1: 1/2 episodes downloaded", msg)
        self.assertIn("S01E02 Next Episode (75.0%)", msg)


class SpyEpisodeSearchSonarr(SonarrClient):
    def __init__(self):
        super().__init__({}, "sonarr")
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def get_series(self, tvdb_id: int) -> dict[str, Any] | None:
        return {"id": 200, "title": "Search Show", "tvdbId": tvdb_id}

    async def get_episodes(self, series_id: int) -> list[dict[str, Any]] | WebRuntimeError:
        return [
            {"id": 701, "seasonNumber": 1, "episodeNumber": 1, "title": "Pilot"},
            {"id": 702, "seasonNumber": 1, "episodeNumber": 2, "title": "Second"},
        ]

    async def post(self, path: str, *, json_body: Any) -> Any | WebRuntimeError:
        self.post_calls.append((path, json_body))
        return {"name": "EpisodeSearch"}


class TestSonarrEpisodeSearch(unittest.IsolatedAsyncioTestCase):
    async def test_search_episode_triggers_sonarr_command(self):
        client = SpyEpisodeSearchSonarr()
        ctx = FakeCtx()

        result = await client.search_episode(ctx, 12345, 1, 2)

        self.assertTrue(result)
        self.assertEqual(
            client.post_calls,
            [("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [702]})],
        )
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("Triggered search", ctx.messages[0])

    async def test_search_episode_not_found_returns_false(self):
        client = SpyEpisodeSearchSonarr()
        ctx = FakeCtx()

        result = await client.search_episode(ctx, 12345, 9, 9)

        self.assertFalse(result)
        self.assertEqual(client.post_calls, [])
        self.assertEqual(len(ctx.messages), 1)
        self.assertIn("Could not find", ctx.messages[0])
