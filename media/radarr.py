from typing import Any

from discord import Embed
from discord.ext import commands

from media.util import AlreadyAddedError, ArrClient, WebRuntimeError


class RadarrClient(ArrClient):
    def __init__(self, config: dict[str, Any], name: str):
        super().__init__(config, name, "tmdb")

    async def lookup(self, term: str) -> list[dict[str, Any]] | WebRuntimeError:
        return await self.get("/api/v3/movie/lookup", params={"term": term})

    async def add_media(
        self,
        media_id: int,
        title: str,
        root_folder: str,
    ) -> dict[str, Any] | WebRuntimeError:
        body = {
            "tmdbId": media_id,
            "title": title,
            "qualityProfileId": self.quality_profile_id,
            "rootFolderPath": root_folder,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
        }
        return await self.post("/api/v3/movie", json_body=body)

    async def get_movie(self, movie_id: int) -> dict[str, Any] | WebRuntimeError:
        return await self.get(f"/api/v3/movie/{movie_id}")

    async def movie_add(
        self,
        ctx: commands.Context,
        tmdb_id: int,
    ) -> bool:
        add_movie = await self.find_and_add(tmdb_id)

        if isinstance(add_movie, AlreadyAddedError):
            await ctx.send(f"❌ **{add_movie.show_details['title']}** already added")
            return False

        if not add_movie or isinstance(add_movie, WebRuntimeError):
            self.log.error("Error while adding movie TMDB ID %s", tmdb_id)
            if isinstance(add_movie, WebRuntimeError):
                self.log.error(add_movie.text)
            await ctx.send(
                (
                    f"❌ No show found with TMDB ID `{tmdb_id}`.\n"
                    "🔎 Please check [TheTMDB](https://www.themoviedb.com)."
                )
            )
            return False

        await ctx.send(
            f"✅ Added movie: **{add_movie['title']}** to folder "
            f"`{self.shorten_path(add_movie['rootFolderPath'])}`"
        )
        return True

    async def movie_download_queue(self, ctx: commands.Context):
        items = await self.queue()
        shows: dict[str, list[str]] = {}

        async def movie_info(mov_id: int) -> tuple[str, str]:
            mov = await self.get_movie(mov_id)
            if isinstance(mov, WebRuntimeError):
                return ("Unknown Movie", "Unknown Year")
            mov_title = mov.get("title", "Untitled")
            mov_year = str(mov.get("year", "????"))
            tmdb_id = mov.get("tmdbId", "-")
            return (f"{mov_title} - tmdbId:{tmdb_id}", mov_year)

        if isinstance(items, WebRuntimeError):
            self.log.error(items.message)
            await ctx.send("Radarr queue fetch failed.")
            return

        for q in items:
            if q.get("sizeleft", 0) <= 0:
                continue
            size = float(q.get("size", 0))
            sizeleft = float(q.get("sizeleft", 0))
            progress = 0.0 if size <= 0 else 100.0 * (size - sizeleft) / size
            movie_id = q.get("movieId")
            movie_title, year = (
                await movie_info(int(movie_id)) if movie_id else ("Unknown Movie", "Unknown Year")
            )
            shows.setdefault(movie_title, []).append(f"{year} ({progress:.1f}%)")

        if not shows:
            await ctx.send("No active Radarr downloads.")
            return

        download_embeds: list[Embed] = []
        for show, lines in shows.items():
            emb = Embed(title=show, description="\n".join(lines), color=0x3498DB)
            download_embeds.append(emb)
        await self._send_embeds_in_batches(ctx, download_embeds)

    async def movie_lookup(self, ctx: commands.Context, query: str):
        """
        Arguments:
        query (str): Movie name to find
        """
        results = await self.lookup(query)
        if isinstance(results, WebRuntimeError):
            self.log.error("Radarr search failed: %s", results.message)
            await ctx.send(f"Radarr search failed: {results.message}")
            return

        if not results:
            await ctx.send(f"No movies found for `{query}`.")
            return

        header = Embed(title=f"Search results for: {query}", color=0x3498DB)
        await ctx.send(embed=header)
        search_embeds: list[Embed] = []

        for l_movie in results[:20]:  # limit to first 20
            title = l_movie.get("title", "Unknown Title")
            year = l_movie.get("year", "N/A")
            tmdb_id = l_movie.get("tmdbId", "N/A")
            genres = ", ".join(l_movie.get("genres", [])) or "N/A"
            tmdb_link = (
                f"https://www.themoviedb.org/movie/{tmdb_id}" if tmdb_id != "N/A" else "N/A"
            )

            embed = Embed()
            embed.add_field(
                name=f"{title} ({year})",
                value=f"🔗[TMDB]({tmdb_link})\nID: `{tmdb_id}`\nGenres: {genres}",
                inline=False,
            )
            if l_movie.get("remotePoster"):
                embed.set_thumbnail(url=l_movie.get("remotePoster"))
            search_embeds.append(embed)

        await self._send_embeds_in_batches(ctx, search_embeds)
