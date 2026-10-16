# Trade Hub — analyst trade tracker for Discord

Each analyst posts trades from their own channel. The bot:

- posts a **trade card** (entry, stop, targets, notes, chart) in the analyst's channel
- mirrors it to a central **hub channel** with a link back to the original
- has a **📊 Trade Board** page that opens inside Discord (a Discord Activity): every open trade with a live price track from stop to final target, plus a detail view with the chart, levels, notes and history
- watches **live prices** (Binance, falling back to Bybit) and marks targets hit, stops hit and limit fills automatically, using 1-minute candle wicks so nothing is missed between checks
- pings an **alerts** channel / role for new trades and closes
- keeps a **leaderboard** of closed trades with `/stats`

## How analysts use it

1. In their channel: `/trade pair:VVV direction:Long` (add `order:Limit` if waiting for a fill)
2. A form pops up: **Entry** (blank = current price), **Stop**, **Targets**, **Notes**, **Chart** upload
3. Targets go one per line with an optional % to close:
   ```
   17 50%
   19.5 25%
   ```
   No % = split evenly. If the % add to less than 100, the rest is a runner that rides until the stop or a manual close.

After that it's automatic. When T1 hits, the stop moves to breakeven (turn off with `/setup breakeven enabled:False`).

Analysts can also manage their own trades (trade picker autocompletes):

| Command | What it does |
|---|---|
| `/move-stop trade price` | Trail the stop. `be` = breakeven. Card shows what it locks in |
| `/take-profit trade percent [price]` | Scale out manually (blank price = market) |
| `/close trade [price]` | Close the rest. Cancels a pending limit |
| `/cancel trade` | Void a mistaken post — excluded from stats |
| `/update trade text` | Post a comment under the trade |
| `/stats [days] [analyst]` | Leaderboard: total R, win rate, avg, best |

**How R is counted:** 1R = distance from entry to the *original* stop. Result = sum over each slice of (% of position × R where it closed). E.g. 50% at +1.3R, 25% at +2.4R, 25% stopped at +3.6R → **+2.15R**. If one candle touches both the stop and a target, the bot assumes the stop came first.

## Setup (≈15 minutes)

> **On a VPS?** Follow [DEPLOY.md](DEPLOY.md) — the full server setup (service, HTTPS, Trade Board) step by step.

### 1. Create the bot
1. Go to <https://discord.com/developers/applications> → **New Application** → name it.
2. **Bot** tab → **Reset Token** → copy the token (keep it secret).
3. **OAuth2 → URL Generator**: tick `bot` and `applications.commands`. Bot permissions: *View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Mention Everyone* (only needed so it can ping alert roles — alternatively make those roles mentionable).
4. Open the generated URL and add the bot to your server.

No privileged intents are needed.

### 2. Run it

**Locally (to test):**
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then paste your token in .env
python bot.py
```

**24/7 hosting** — pick one:
- **Railway**: push this folder to a GitHub repo → New Project → Deploy from repo → add variable `DISCORD_TOKEN` → add a **Volume** mounted at `/app/data` (so trades survive redeploys). Start command: `python bot.py`.
- **Any VPS** (Hetzner / DigitalOcean, ~$5/mo): copy the folder, install as above, run it under `systemd` or `pm2 start bot.py --interpreter python3`.

⚠️ Host it **outside the US** (Binance and Bybit block US server IPs). EU/Asia regions are fine.

All data lives in `data/` (SQLite database + saved charts) — back that folder up.

### 3. Configure in Discord (admins / Manage Server)
1. Create channels, e.g. `#trade-hub` (mirror feed), `#open-trades` (the board), `#alerts`, and one per analyst.
2. `/setup channels hub:#trade-hub board:#open-trades alerts:#alerts alerts_role:@Alerts name:Haven`
3. For each analyst: `/analyst add channel:#pierre name:Pierre user:@Pierre alert_role:@Alerts - Pierre`
   (`user` locks posting to that person; `alert_role` lets members follow individual analysts.)
4. Lock the hub and board channels so only the bot can post.

Tip: set `GUILD_ID` in `.env` to your server ID while testing so slash commands appear instantly (global ones can take up to an hour the first time).

## Trade Board page (Discord Activity)

The board is a web page the bot serves, which Discord opens full-screen inside the app. The `#open-trades` channel gets one small card with an **Open Trade Board** button. Only members of your server can load its trades.

After step 2 you can preview the page with sample data in any browser at `https://trades.yourdomain.com/?demo`.

**1. Point a subdomain at the VPS.** At your DNS provider add an **A record**: `trades` → your VPS's IP.

**2. Put HTTPS in front of the bot with Caddy** (it gets the certificate automatically):
```bash
sudo apt install -y caddy
echo 'trades.yourdomain.com {
    reverse_proxy 127.0.0.1:8080
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```
Open ports 80 and 443 if you use a firewall (`sudo ufw allow 80,443/tcp`).

**3. Developer Portal** (<https://discord.com/developers/applications> → your app):
- **OAuth2** → *Reset Secret*, put it in `.env` as `DISCORD_CLIENT_SECRET`. Under *Redirects* add `https://127.0.0.1` (Discord requires one; it isn't used).
- **Activities → URL Mappings** → *Root Mapping*: `trades.yourdomain.com` (no `https://`).
- **Activities → Settings** → turn on *Enable Activities* and tick the platforms (desktop, iOS, Android).

**4. Restart the bot** (`sudo systemctl restart tradehub`) and run `/setup channels` if you haven't — the button card appears in your board channel.

## Files
| File | Purpose |
|---|---|
| `bot.py` | Discord commands, the trade form, price loop, board |
| `tracker.py` | Trade maths: parsing, R, targets/stops, formatting (no Discord code) |
| `prices.py` | Binance → Bybit 1-minute candles, no API keys |
| `render.py` | The embeds: trade card, board button card, leaderboard |
| `webapp.py` | Web server + API for the Trade Board page (Discord sign-in, members only) |
| `web/static/` | The Trade Board page itself (HTML/CSS/JS + Discord's Activity SDK, bundled) |
| `db.py` | SQLite storage |
| `tests/` | `python -m pytest tests` |

## Limits worth knowing
- Crypto only (any pair on Binance spot or Bybit spot/perps). Stocks/forex would need another price source in `prices.py`.
- If the bot is offline more than ~16 hours, wicks during the gap aren't replayed — check open trades after long downtime.
- The Trade Board page refreshes every 15s; the button card every 60s; trade cards every 5 min (instantly on any hit).
