import unittest
from typing import Any

from media.util import AlreadyAddedError, ArrClient, WebRuntimeError


class FakeArrClient(ArrClient):
    def __init__(self):
        super().__init__({"default_folder": "/media/default"}, "fake", "tvdb")
        self.lookup_result: list[dict[str, Any]] | WebRuntimeError = []
        self.add_result: dict[str, Any] | WebRuntimeError = {}
        self.add_calls: list[tuple[int, str, str]] = []

    async def lookup(self, term: str) -> list[dict[str, Any]] | WebRuntimeError:
        return self.lookup_result

    async def add_media(
        self, media_id: int, title: str, root_folder: str
    ) -> dict[str, Any] | WebRuntimeError:
        self.add_calls.append((media_id, title, root_folder))
        return self.add_result


class TestFindAndAdd(unittest.IsolatedAsyncioTestCase):
    async def test_find_and_add_returns_none_on_empty_lookup(self):
        client = FakeArrClient()
        client.lookup_result = []

        result = await client.find_and_add(123)

        self.assertIsNone(result)
        self.assertEqual(client.add_calls, [])

    async def test_find_and_add_returns_already_added_error_on_400(self):
        client = FakeArrClient()
        client.lookup_result = [{"title": "Show", "genres": []}]
        client.add_result = WebRuntimeError("post", "already exists", 400)

        result = await client.find_and_add(456)

        self.assertIsInstance(result, AlreadyAddedError)
        self.assertEqual(len(client.add_calls), 1)
