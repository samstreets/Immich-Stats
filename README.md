# Immich Monitor

A lightweight self-hosted dashboard for your [Immich](https://immich.app) photo server. It tracks asset counts, disk usage, background jobs, named people, and user quotas — and stores hourly snapshots so you can watch trends over time.

![screenshot placeholder](https://placehold.co/1200x600/0a0a0b/f97316?text=Immich+Monitor)

---

## Features

- **Live stats** — total assets, photos, videos, albums, users, and named faces
- **Disk usage** — real-time bar with colour-coded warnings (yellow > 75 %, red > 90 %)
- **Background jobs** — per-queue active / waiting / failed counts
- **User storage quotas** — per-user usage bars (shown only when quotas are set)
- **Top people** — ranked bar chart of your most-photographed named faces
- **Trend charts** — asset growth and disk usage over 1 h / 6 h / 24 h / 7 d / all time
- **Historical snapshots** — hourly records stored in SQLite, pruned to one-per-week after 30 days
- **Auto-refresh** — polls every 60 minutes; manual refresh button always available

---

## Quick start

### Docker Compose (recommended)

```yaml
services:
  immich-monitor:
    image: samuelstreets/immich-monitor:latest
    container_name: immich-monitor
    restart: unless-stopped
    ports:
      - "8765:8765"
    volumes:
      - immich_monitor_data:/data

volumes:
  immich_monitor_data:
    driver: local
```

```bash
docker compose up -d
```

Then open **http://localhost:8765** in your browser.

### Build from source

```bash
git clone https://github.com/samstreets/immich-monitor.git
cd immich-monitor
docker compose up -d --build
```

---

## Configuration

On first launch you'll see the **Connect to Immich** setup screen.

| Field | Example | Notes |
|---|---|---|
| Immich Server URL | `http://192.168.1.100:2283` | No trailing slash |
| API Key | `abc123...` | Account Settings → API Keys in Immich |

> **Admin API key recommended.** A regular user key will still show asset/disk stats, but job queue data requires an admin key.

Click **Test Connection** to verify before saving.

---

## Running without Docker

Requires Python 3.12+.

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8765
```

The SQLite database is written to `/data/immich_history.db` if that path is writable, otherwise alongside `main.py`.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/config` | Returns saved URL (key never returned) |
| `POST` | `/api/config` | Save Immich URL + API key |
| `POST` | `/api/test` | Test a connection without saving |
| `GET` | `/api/status` | Live data from Immich + write snapshot |
| `GET` | `/api/history?limit=500` | Recent snapshots (newest last) |
| `GET` | `/api/history/range?from_ts=&to_ts=` | Snapshots in a timestamp range (ms) |
| `GET` | `/api/debug/db` | DB health check |
| `GET` | `/api/debug/people` | Raw `/people` response from Immich |

---


## Data retention

| Age | Resolution |
|---|---|
| 0 – 30 days | Every hour |
| 30 days + | One snapshot per calendar week |

Pruning runs automatically at startup and once every 24 hours.

---

## Project structure

```
immich-monitor/
├── .github/
│   └── workflows/
│       └── ci.yml          # Lint → build → push → tag
├── backend/
│   ├── main.py             # FastAPI backend
│   ├── index.html          # Single-file frontend
│   └── requirements.txt
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

## Troubleshooting

**"Failed to save" on the setup screen**
Ensure you are running the fixed version of `index.html` (the `REFRESH_INTERVAL` constant was previously declared after the variable that referenced it, causing a `ReferenceError` in strict mode).

**Jobs section shows "No job data"**
The `/api/jobs` endpoint in Immich requires an admin API key. Generate one under **Administration → API Keys**.

**People section is empty**
Immich must have face recognition enabled and at least one person named in the People view.

**Cannot connect to Immich**
- Check the URL includes the port (default `2283`)
- Make sure the monitor container can reach your Immich host (same Docker network, or LAN IP)
- Try `curl http://<immich-host>:2283/api/server/about` from inside the monitor container

---

## License

MIT
