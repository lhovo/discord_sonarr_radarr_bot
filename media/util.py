import asyncio
import logging
from pathlib import Path
import json
from abc import ABC, abstractmethod
from typing import Any, Literal
import aiohttp
from discord import Embed, HTTPException
from discord.ext import commands

class WebBaseError():
    def __init__(self, message: str, status_code: int):
        self.message = message  # Call the parent class's constructor
        self.status_code = status_code  # Http status code

class WebRuntimeError(WebBaseError):
    def __init__(self, message: str, text: str, status_code: int):
        super().__init__(message, status_code)
        self.text = text

class AlreadyAddedError(WebBaseError):
    def __init__(self, message: str, status_code: int, show_details: dict[str, Any]):
        super().__init__(message, status_code)
        self.show_details = show_details

class HttpClient:
    def __init__(self, timeout_s: float = 30.0):
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected: set[int] | None = None,
        retries: int = 2,
        backoff_s: float = 0.5,
    ) -> aiohttp.ClientResponse:
        exc: Exception | None = None
        expected = expected or {200}
        for attempt in range(retries + 1):
            try:
                if self._session is None:
                    continue
                resp = await self._session.request(
                    method, url, headers=headers, params=params, json=json_body
                )
                if resp.status in expected:
                    return resp
                if resp.status in {429, 500, 502, 503, 504} and attempt < retries:
                    await asyncio.sleep(backoff_s * (2 ** attempt))
                    continue
                return resp
            except aiohttp.ClientConnectorError as e:
                print('Connection Error', str(e))
                exc = e
                if attempt < retries:
                    await asyncio.sleep(backoff_s * (2 ** attempt))
                    continue
                break
            except aiohttp.ClientResponseError as e:
                print('Status: ', str(e.status))
                exc = e
                if attempt < retries:
                    await asyncio.sleep(backoff_s * (2 ** attempt))
                    continue
                break
        assert exc is not None
        raise exc

class ArrClient(ABC):
    def __init__(self, config: dict[str, Any], name: str, lookup_query:str):
        self.base_url: str = config.get("url", "").rstrip("/")
        self.api_key: str = config.get("api_key", "")
        self.quality_profile_id: int = int(config.get("quality_profile_id", 6))
        self.folders: list[dict[str, str]] | None = config.get("folders", None)
        self.default_folder: str = config.get("default_folder", "")
        self.name = name
        self.headers: dict[str, str] = {"X-Api-Key": self.api_key}
        self.log = logging.getLogger(f"arr.{name}")
        self.lookup_query = lookup_query

    async def get(self,
                  path: str,
                  *,
                  params: dict[str, Any] | None = None
                  ) -> Any | WebRuntimeError:
        url = f"{self.base_url}{path}"

        async with HttpClient(timeout_s=30.0) as session:
            try:
                response = await session.request(
                    "GET", url, headers=self.headers, params=params
                )
            except aiohttp.ClientError as e:
                return WebRuntimeError(f"{self.name} GET {path}", str(e), 0)
            self.log.info("Status for %s: %s", url, response.status)
            self.log.info("Content-type: %s", response.headers.get('Content-Type'))

            if response.status != 200:
                text = await response.text()
                return WebRuntimeError(f"{self.name} GET {path}",
                                       text, response.status)

            content = await response.json()
            self.log.debug("Body GET: %s", json.dumps(content))

            return content

    async def post(self, path: str, *, json_body: Any) -> Any | WebRuntimeError:
        url = f"{self.base_url}{path}"
        # Accept multiple success codes (Radarr/Sonarr can return 201 on create)

        async with HttpClient(timeout_s=30.0) as session:
            try:
                response = await session.request(
                    "POST",
                    url,
                    headers=self.headers,
                    json_body=json_body,
                    expected={200, 201, 400}
                )
            except aiohttp.ClientError as e:
                return WebRuntimeError(f"{self.name} POST {path}", str(e), 0)
            self.log.info("Status for %s: %s", url, response.status)
            self.log.info("Content-type: %s", response.headers.get('Content-Type'))

            if response.status not in {200, 201}:
                text = await response.text()
                self.log.debug("Body POST (first 100 chars): %s", text[:100])
                return WebRuntimeError(f"{self.name} POST {path}",
                                       text, response.status)
            if response.content_length == 0:
                return None
            content = await response.json()
            self.log.debug("Body POST: %s...", json.dumps(content))
            return content

    def pick_root_folder(self, genres: list[str]) -> str:
        """
        Pick a root folder path for a given media type based on its genres.

        Args:
            media_type (str): Either "sonarr" or "radarr".
            genres (list[str]): A list of genre strings.

        Returns:
            str: Path to the selected root folder.
        """

        genres_lower = [g.lower() for g in genres]

        if self.folders:
            for rule in self.folders:
                if any(keyword in genres_lower for keyword in rule["keywords"]):
                    return rule["folder"]

        return self.default_folder

    def shorten_path(self, path: str) -> str:
        """Return only the final component of a filesystem path."""
        return Path(path).name

    async def queue(self, page_size: int = 500) -> list[dict[str, Any]] | WebRuntimeError:
        data = await self.get("/api/v3/queue", params={"pageSize": page_size})
        if isinstance(data, WebRuntimeError):
            return data
        return list(data.get("records", []))

    @abstractmethod
    async def lookup(self, term: str) -> list[dict[str, Any]] | WebRuntimeError:
        pass

    @abstractmethod
    async def add_media(
        self,
        media_id: int,
        title: str,
        root_folder: str,
    ) -> dict[str, Any] | WebRuntimeError:
        pass

    async def find_and_add(
            self,
            find_id: int,
    ) -> dict[str, Any] | WebRuntimeError | AlreadyAddedError | None:
        media_lookup = await self.lookup(f"{self.lookup_query}:{find_id}")
        if isinstance(media_lookup, WebRuntimeError):
            return media_lookup
        if not media_lookup:
            return None
        media = media_lookup.pop(0)
        root_folder = self.pick_root_folder(media.get("genres", []))
        add_post = await self.add_media(find_id, media["title"], root_folder)

        if isinstance(add_post, WebRuntimeError):
            if add_post.status_code == 400:
                self.log.error(add_post.text)
                return AlreadyAddedError(add_post.message, add_post.status_code, media)
            return add_post
        if isinstance(add_post, list):
            return add_post[0]
        return add_post

    async def _send_embeds_in_batches(
            self,
            ctx: commands.Context,
            embeds: list[Embed],
            batch_size: int = 10
        ) -> None:
        for i in range(0, len(embeds), batch_size):
            try:
                await ctx.send(embeds=embeds[i : i + batch_size])
            except HTTPException as e:
                self.log.error("Failed to send embeds: %s", e)
                # Fallback to one-by-one if bulk send fails
                for emb in embeds[i : i + batch_size]:
                    try:
                        await ctx.send(embed=emb)
                    except HTTPException as e2:
                        self.log.error("Failed to send single embed: %s", e2)
