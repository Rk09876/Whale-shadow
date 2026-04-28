# Whale Shadow Tracker

> _Cross the deep — in the wake of giants._

Free, legal, self-hosted institutional-flow heatmap for **NIFTY** and **SENSEX**
options. Built for the retail trader who wants to see where the whales are
positioned without buying a TBT feed they couldn't act on anyway.

---

## What it shows

- **Strike × time OI heatmap** — color-coded by tier:
  - `shoal` (bone) — normal OI
  - `shark` (amber) — large OI cluster (>1.5σ from cross-strike mean)
  - `whale` (magenta) — institutional wall (>3σ)
- **Spot price** drawn as a teal dashed line across the heatmap
- **Max pain** drawn as a magenta dashed line (option writers' preferred close)
- **Walls panel** — top 8 CE (resistance) and PE (support) walls right now
- **Timing strip** — current session phase, minutes to close, and Vedic hora
- **Whale wake** — FII/DII end-of-day flows and same-day block deals

All data comes from **public NSE & BSE endpoints**. Zero credentials, zero
broker integration, zero payments. Everything is read-only.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  scheduler  ──poll──►  scrapers/{nse,bse,flows}  ──parse──►      │
│                                                                  │
│  parsed snapshot  ──►  analytics/walls.detect_walls()            │
│                                                                  │
│         ▼                                                        │
│  storage/db (SQLite, persistent on Render disk)                  │
│         ▼                                                        │
│  FastAPI  ──serves──►  /api/heatmap, /api/walls, /api/timing …   │
│         ▼                                                        │
│  static/index.html + app.js  (D3 heatmap, polls every 15s)       │
└─────────────────────────────────────────────────────────────────┘
```

## Deploy on Render (one-click)

1. Push this repo to GitHub.
2. Go to [render.com](https://render.com) → New → Blueprint → connect repo.
3. Render reads `render.yaml` and provisions the service + persistent disk.
4. First deploy takes ~3 min. Then visit the URL on your phone.
5. Scheduler starts polling automatically during NSE trading hours
   (Mon–Fri 09:00–16:00 IST). The heatmap fills in after ~2 polls.

> Free tier note: Render free instances sleep after 15 min of no traffic.
> The scheduler keeps running on a paid instance, but on free tier you'll
> miss snapshots when the service sleeps. Cheapest fix: $7/mo Starter
> plan, or schedule a free uptime ping (e.g., UptimeRobot every 10 min).

## Run locally

```bash
# 1. Clone
git clone <your-repo> whale_shadow && cd whale_shadow

# 2. Either docker-compose...
docker compose up --build

# 3. ...or python venv
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 on any device on your network.

## How to read it

**Fresh deploys see "collecting snapshots…" for ~1 minute.** This is
normal — the heatmap needs at least 2 snapshots before any time-series
exists. After that the rightmost column is the most recent poll.

**Magenta cells = whales committed.** A magenta CE strike sitting above
spot is a resistance wall; a magenta PE strike below spot is a support
wall. Watch the top of the visible range: if the highest magenta CE wall
is breached, the market has crossed an institutional commitment level.

**Magenta cells melting into shark/shoal between polls** = whales unwinding.
A wall dissolving an hour before close often signals positioning into the
next session.

**Spot line crossing a wall column** = the actual test moment. That's the
candle to watch on your normal chart.

## Configuration

Edit `render.yaml` env vars (or `.env` locally):

| Variable | Default | Meaning |
|---|---|---|
| `POLL_SECONDS` | `30` | option chain poll cadence |
| `FLOWS_INTERVAL_SECONDS` | `1800` | FII/DII + block deals refresh |
| `TRADING_HOURS_ONLY` | `1` | only poll Mon–Fri 09:00–16:00 IST |
| `SNAPSHOTS_KEEP` | `500` | per-symbol DB retention |

## Known caveats

- **BSE option chain endpoint shape changes** without notice. The parser
  is defensive (handles multiple known shapes) but if SENSEX rendering
  ever shows zero strikes, check the network tab on bseindia.com and
  patch `app/scrapers/bse.py`.
- **NSE blocks aggressive scrapers**. Default 30s cadence is well within
  the polite window. Don't drop below 15s or your IP will get throttled.
- **Free tier latency**: Render's Singapore region is ~80ms RTT to NSE.
  Good enough for 30s polling.
- **No after-hours snapshots** — by design. Scheduler skips outside
  trading hours to save bandwidth.
- **SENSEX expiries are weekly Tuesdays** since BSE's 2024 reschedule.
  NIFTY moved to weekly Thursdays. The dashboard expiry dropdown
  reflects whatever the exchange currently publishes.

## What this is NOT

- Not a broker. It does not place orders.
- Not a tip service. It shows positioning, not predictions.
- Not financial advice. It's a tool for seeing what's already there.

## Layers you can add later

The codebase is intentionally modular. Easy extensions:

- **Alerts** — POST to a webhook when whale-grade walls form near your
  positions. Hook into `analytics/walls.detect_walls` output in `scheduler.py`.
- **AGIF overlay** — drop a JSON file of hora/Gann time markers into
  `data/agif.json`; add an endpoint that serves it; render dashed
  vertical lines on the heatmap.
- **Range-guard band** — pre-compute your day-range estimate elsewhere,
  POST it to a `/api/range-guard` endpoint, render as a horizontal band.
- **BANKNIFTY / FINNIFTY** — add to `SUPPORTED_SYMBOLS` and route to the
  NSE scraper with the right symbol parameter.
- **Historical replay** — the SQLite file is portable. Open it in
  DuckDB or pandas and replay a day's snapshots offline.

## License

MIT. Use it, fork it, share it. If it saves a retail trader's account,
that's the whole point.
