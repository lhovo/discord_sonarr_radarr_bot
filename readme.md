# 🎬 Discord Media Bot

A Discord bot that integrates with **Sonarr** and **Radarr** to search, add, and track the progress of movies and TV shows.  
The bot supports notifications, downloading status, custom commands, and configurable filters for genres like **documentaries** or **children’s shows**.

---

## 🚀 Features

- Search for movies and TV shows via **TMDb**.
- Add series or movies directly to **Sonarr** or **Radarr**.
- Track active downloads and show progress (episodes + movies).
- Webhook integration for notifications when downloads complete.
- Configurable **root folders**, **quality profiles**, and **restricted channels**.
- Customizable help command with aliases.
- Runs easily in **Docker**.

---

## 📦 Requirements

- Python **3.9+**
- Dependencies (see `requirements.txt`):
  - `discord.py`
  - `PyYAML`
  - `requests`
  - `aiohttp`
  - `flask` (for webhook support)
  - `asyncio`

Or install with:

```bash
pip install -r requirements.txt
```

## ⚙️ Example config.yml
```yml
discord:
  token: "YOUR_DISCORD_BOT_TOKEN"
  prefix: "!"
  restricted_channels:
    - 123456789012345678   # Only allow commands in these channels, the first channel is used to post sonnar and radarr events

sonarr:
  url: "http://sonarr:8989"
  api_key: "YOUR_SONARR_API_KEY"
  folders:
    kids: "/media/kids_tv"
    documentary: "/media/documentaries_tv"
    default: "/media/tv"
  quality_profile_id: 6   # Default quality profile

radarr:
  url: "http://radarr:7878"
  api_key: "YOUR_RADARR_API_KEY"
  folders:
    kids: "/media/kids_movies"
    documentary: "/media/documentaries_movies"
    default: "/media/movies"
  quality_profile_id: 6

settings:
  debounce_time: 5   # seconds for webhook debounce
  notify_on_add: true
  genres:
    documentaries: true
    children: true
logging:
  level: INFO
  file: "logs/bot.log"
```

## 🐳 Running with Docker

Build the image:
```bash
docker build -t discord-media-bot .
```

Run the container:
```bash
docker run -d \
  --name discord-bot \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/logs:/app/logs:rw \
  --restart unless-stopped \
  -p 5000:5000 \
  discord-media-bot
```

If Sonarr and Radarr are in the same Docker network, you can reference them by container name (e.g. http://sonarr:8989).

## 🔔 Webhook Setup
- In Sonarr and Radarr, configure a Webhook notification pointing to your bot container:
```bash
http://your-bot:5000/webhook
```

- Events (grabbed, downloaded, failed) will be sent to the bot.
- The bot will debounce duplicate events to avoid spam and respect rate limits.

## 📖 Example Commands
- !find the matrix → Search for “The Matrix”.
- !addseries <tvdb_id> → Add a new TV series to Sonarr.
- !addmovie <tmdb_id> → Add a movie to Radarr.
- !progress → Show all active downloads.
- !status <tvdb_id> → Show detailed status of a show.

## 📝 Development

Run locally:
```bash
python discord_media.py
```
To auto-reload during development:
```bash
pip install watchdog
watchmedo auto-restart -d . -p "*.py" -- python discord_media.py
```

## 📜 License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
