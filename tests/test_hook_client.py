import unittest
from unittest.mock import patch

from hook_client import WebServer


class FakeRequest:
    def __init__(self, payload: dict):
        self._payload = payload
        self.headers: dict[str, str] = {}

    async def json(self):
        return self._payload


def _sonarr_payload(series_title: str, season: int, episode: int, event_type: str = "Download") -> dict[str, str | dict[str, str | int] | list[dict[str, int]]]:
    return {
        "eventType": event_type,
        "series": {
            "title": series_title,
            "tvdbId": 1234,
        },
        "episodes": [
            {
                "seasonNumber": season,
                "episodeNumber": episode,
            }
        ],
    }


class TestWebhookSonarrDebounce(unittest.IsolatedAsyncioTestCase):
    async def test_sonarr_download_waits_and_resets_for_new_event(self):
        server = WebServer({})

        with patch.object(server, "_now", return_value=0.0):
            await server.handle_event(FakeRequest(_sonarr_payload("Show A", 1, 1)))

        with patch.object(server, "_now", return_value=9.0):
            self.assertIsNone(await server.schedule_send())

        with patch.object(server, "_now", return_value=9.5):
            await server.handle_event(FakeRequest(_sonarr_payload("Show A", 1, 2)))

        with patch.object(server, "_now", return_value=18.0):
            self.assertIsNone(await server.schedule_send())

        with patch.object(server, "_now", return_value=20.0):
            embed = await server.schedule_send()

        self.assertIsNotNone(embed)
        assert embed is not None
        self.assertIn("S01E01", embed.description)
        self.assertIn("S01E02", embed.description)

    async def test_sonarr_download_flushes_after_one_minute_max(self):
        server = WebServer({})

        with patch.object(server, "_now", return_value=0.0):
            await server.handle_event(FakeRequest(_sonarr_payload("Show B", 1, 1)))

        for idx, ts in enumerate([9.0, 18.0, 27.0, 36.0, 45.0, 54.0], start=2):
            with patch.object(server, "_now", return_value=ts):
                await server.handle_event(FakeRequest(_sonarr_payload("Show B", 1, idx)))

        with patch.object(server, "_now", return_value=59.0):
            self.assertIsNone(await server.schedule_send())

        with patch.object(server, "_now", return_value=60.0):
            embed = await server.schedule_send()

        self.assertIsNotNone(embed)
        assert embed is not None
        self.assertEqual(embed.description.count("\n") + 1, 7)
