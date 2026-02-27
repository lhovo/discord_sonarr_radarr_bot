# Discord Media Bot

A Discord bot that integrates with Sonarr and Radarr to search, add, and track movie/TV downloads, plus post webhook updates to Discord.

## Requirements

- Python 3.12+
- A `config.yaml` file in the project root
- Sonarr and Radarr API access
- Discord bot token with message content intent enabled

Install dependencies:

```bash
pip install -r requirements.txt
```

## Config Example (`config.yaml`)

```yaml
discord:
  token: "YOUR_DISCORD_BOT_TOKEN"
  prefix: "!"
  # Only allow commands in these channels
  # The first channel is used to post sonnar and radarr events
  restricted_channels:
    - 123456789012345678

sonarr:
  url: "http://sonarr:8989"
  api_key: "YOUR_SONARR_API_KEY"
  quality_profile_id: 6 # Default quality profile
  default_folder: "/media/kids_tv"
  folders:
    - keywords: ["kids", "children"]
      folder: "/media/kids_tv"
    - keywords: ["documentary"]
      folder: "/media/documentaries_tv"

radarr:
  url: "http://radarr:7878"
  api_key: "YOUR_RADARR_API_KEY"
  quality_profile_id: 6 # Default quality profile
  default_folder: "/media/movies"
  folders:
    - keywords: ["kids", "children"]
      folder: "/media/kids_movies"
    - keywords: ["documentary"]
      folder: "/media/documentaries_movies"

webhook:
  host: "0.0.0.0"
  port: 5000
  secret: ""
  recent_ttl_seconds: 600

logging:
  level: "INFO"
  file: "/app/logs/bot.log"
```

## Commands

- `!tv` - Show active Sonarr downloads
- `!tv <query>` - Search Sonarr series
- `!tv <tvdbId>` - Show per-season status for a series
- `!tvadd <tvdbId>` - Add series to Sonarr
- `!movie` - Show active Radarr downloads
- `!movie <query>` - Search Radarr movies
- `!movieadd <tmdbId>` - Add movie to Radarr

## Run Locally

```bash
python discord_media.py
```

## Docker Compose

Use the included `docker_compose.yml`:

```bash
docker compose up -d --build
```

Stop:

```bash
docker compose down
```

Notes:

- Container runs as non-root user `1000:1000`.
- Project code is mounted read-only; `./logs` is mounted read-write.
- Healthcheck verifies the configured webhook port is reachable.

## Webhook URL

Set Sonarr/Radarr webhook to:

```text
http://<bot-host>:5000/webhook
```

If you change `webhook.port` in `config.yaml`, update published ports and webhook URL accordingly.

## License

GNU General Public License v3.0. See `LICENSE`.
