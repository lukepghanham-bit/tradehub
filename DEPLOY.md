# Deploying Trade Hub on a VPS

Step-by-step setup for an Ubuntu/Debian VPS. Written so a person **or Claude Code running on the VPS** can follow it top to bottom.

Placeholders used below — replace them everywhere:

| Placeholder | Meaning |
|---|---|
| `trades.example.com` | The subdomain for the Trade Board page |
| `/opt/tradehub` | Where the bot is installed |
| `tradehub` | The Linux user the bot runs as |

## Rules for whoever runs this

- **Never print, log, echo or ask for secrets in chat.** `DISCORD_TOKEN` and `DISCORD_CLIENT_SECRET` are typed into `.env` by the owner. Create the file with empty values and ask them to fill it in with `sudo nano /opt/tradehub/.env`.
- **Check each step before moving on** (each step lists a check). If a check fails, stop and fix it — don't skip ahead.
- **The VPS must not be in the US.** Binance and Bybit block US IPs, so prices won't load. Check with `curl -s https://ipinfo.io/country` (needs to print something other than `US`).
- Don't touch other sites/services on the server. If Caddy, nginx or Apache is already serving other sites, **add** to the existing config instead of replacing it (see step 6).

## What only the owner can do

These happen outside the VPS. The rest of this guide can proceed while they're pending; steps that depend on them say so.

1. **DNS:** add an A record `trades` → the VPS's public IP at the domain's DNS provider.
2. **Bot token:** Developer Portal → app → **Bot** → *Reset Token* → paste into `.env` as `DISCORD_TOKEN`.
3. **Client secret:** Developer Portal → **OAuth2** → *Reset Secret* → paste into `.env` as `DISCORD_CLIENT_SECRET`.
4. **Developer Portal settings** for the Trade Board:
   - **OAuth2 → Redirects:** add `https://127.0.0.1` (required placeholder, not used).
   - **Activities → URL Mappings → Root Mapping:** `trades.example.com` (no `https://`).
   - **Activities → Settings:** enable Activities; tick desktop, iOS and Android.

---

## 1. Check the server

```bash
curl -s https://ipinfo.io/country; echo      # must NOT be US
python3 --version                            # needs 3.10+
lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2
sudo ss -ltnp | grep -E ':(80|443|8080)\b' || echo "ports 80/443/8080 free"
```
If 8080 is taken, pick another free port and use it for `WEB_PORT` (step 4) and the Caddy config (step 6).
If 80/443 are taken by an existing web server, note which one — step 6 has instructions for each.

## 2. Install packages and create the user

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
sudo useradd --system --create-home --home-dir /opt/tradehub --shell /usr/sbin/nologin tradehub 2>/dev/null || true
```
**Check:** `id tradehub` prints a uid.

## 3. Get the code

The repo `lukepghanham-bit/tradehub` is **private**, so the server needs a read-only deploy key.

```bash
sudo -u tradehub mkdir -p /opt/tradehub/.ssh
sudo -u tradehub ssh-keygen -t ed25519 -N "" -C "tradehub-vps" -f /opt/tradehub/.ssh/deploy_key
sudo cat /opt/tradehub/.ssh/deploy_key.pub
```
Ask the owner to add that **public** key at GitHub → `lukepghanham-bit/tradehub` → Settings → Deploy keys → *Add deploy key* (leave "Allow write access" off). Wait for them to confirm, then:

```bash
sudo -u tradehub bash -c '
  cd /opt/tradehub
  export GIT_SSH_COMMAND="ssh -i /opt/tradehub/.ssh/deploy_key -o StrictHostKeyChecking=accept-new"
  git clone git@github.com:lukepghanham-bit/tradehub.git app
  git -C app config core.sshCommand "ssh -i /opt/tradehub/.ssh/deploy_key"
'
```
**Check:** `ls /opt/tradehub/app/bot.py` exists.

(The code lives in `/opt/tradehub/app`; paths below use that.)

## 4. Python environment and config

```bash
sudo -u tradehub bash -c '
  cd /opt/tradehub/app
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  cp -n .env.example .env
  chmod 600 .env
'
```
Set `WEB_PORT` if you changed it in step 1:
```bash
sudo -u tradehub sed -i 's/^WEB_PORT=.*/WEB_PORT=8080/' /opt/tradehub/app/.env
```
Now **ask the owner** to open `.env` and paste their bot token and client secret:
```bash
sudo -u tradehub nano /opt/tradehub/app/.env
```
**Check (without revealing values):**
```bash
sudo -u tradehub bash -c 'cd /opt/tradehub/app && for k in DISCORD_TOKEN DISCORD_CLIENT_SECRET; do v=$(grep -E "^$k=" .env | cut -d= -f2-); if [ -z "$v" ] || [ "$v" = "paste-your-bot-token-here" ]; then echo "$k: MISSING"; else echo "$k: set"; fi; done'
```
Run the tests: `sudo -u tradehub bash -c 'cd /opt/tradehub/app && .venv/bin/pip install -q pytest && .venv/bin/python -m pytest -q tests'` → all pass.

## 5. Run it as a service

```bash
sudo tee /etc/systemd/system/tradehub.service >/dev/null <<'EOF'
[Unit]
Description=Trade Hub Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=tradehub
WorkingDirectory=/opt/tradehub/app
ExecStart=/opt/tradehub/app/.venv/bin/python bot.py
Restart=always
RestartSec=5
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now tradehub
```
**Check:**
```bash
sleep 8; sudo journalctl -u tradehub -n 30 --no-pager   # expect "Logged in as Trade Hub#…" and "Trade Board web server on port 8080"
curl -s http://127.0.0.1:8080/api/config               # expect {"client_id": "…"}
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8080/?demo"   # expect 200
```
If the log says "Improper token", the token in `.env` is wrong — ask the owner to reset it and paste again.

## 6. HTTPS for the Trade Board page

Needs the DNS record from "What only the owner can do". **Check DNS first:**
```bash
dig +short trades.example.com     # must print this server's IP (curl -s https://ipinfo.io/ip)
```

**A) Nothing on ports 80/443 yet → install Caddy (handles HTTPS automatically):**
```bash
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
trades.example.com {
    reverse_proxy 127.0.0.1:8080
}
EOF
sudo systemctl reload caddy
```

**B) Caddy already running** → *append* the block above to `/etc/caddy/Caddyfile` (don't replace the file), then `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.

**C) nginx already running** → add a server block and get a certificate with certbot:
```bash
sudo tee /etc/nginx/sites-available/tradehub >/dev/null <<'EOF'
server {
    listen 80;
    server_name trades.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -sf /etc/nginx/sites-available/tradehub /etc/nginx/sites-enabled/tradehub
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d trades.example.com --non-interactive --agree-tos --register-unsafely-without-email --redirect
```
(Ask the owner before agreeing to Let's Encrypt's terms on their behalf, and whether they want an email on the certificate — if so use `-m their@email` instead of `--register-unsafely-without-email`.)

**Firewall** (if `ufw` is active): `sudo ufw allow 80,443/tcp`. Don't expose 8080 — the bot only listens on 127.0.0.1.

**Check:**
```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://trades.example.com/?demo"   # expect 200
```
The owner can open `https://trades.example.com/?demo` in a browser to see the board with sample data.

## 7. Discord setup (owner, in Discord)

Once the Developer Portal settings from "What only the owner can do" are saved:

1. Invite the bot if not done yet (OAuth2 → URL Generator, scopes `bot` + `applications.commands`).
2. Create channels: a hub feed (e.g. `#trade-hub`), the board (`#open-trades`), alerts (`#alerts`), one per analyst.
3. `/setup channels hub:#trade-hub board:#open-trades alerts:#alerts name:YourServerName`
4. `/analyst add channel:#pierre name:Pierre user:@Pierre` — once per analyst.
5. In `#open-trades`, click **Open Trade Board** → the page opens inside Discord.

Slash commands can take up to an hour to appear the first time. For instant commands while testing, set `GUILD_ID=<server id>` in `.env` and `sudo systemctl restart tradehub`.

## Updating

```bash
sudo -u tradehub git -C /opt/tradehub/app pull
sudo -u tradehub /opt/tradehub/app/.venv/bin/pip install -q -r /opt/tradehub/app/requirements.txt
sudo systemctl restart tradehub
```
`.env` and `data/` are git-ignored, so updates never touch the token or the trade history.

## Backups

Everything lives in `/opt/tradehub/app/data/` (SQLite database + chart images). Example nightly backup:
```bash
echo '15 3 * * * tradehub tar czf /opt/tradehub/backup-$(date +\%a).tgz -C /opt/tradehub/app data' | sudo tee /etc/cron.d/tradehub-backup
```
(Keeps a rolling 7 days.)

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot offline | `sudo journalctl -u tradehub -n 50 --no-pager` |
| Prices never update / "Couldn't find XYZUSDT" for every coin | Server is in the US or blocks outbound HTTPS — `curl -sI https://data-api.binance.vision/api/v3/ping` |
| Button says "Trade Board isn't switched on yet" | Activities not enabled in the Developer Portal, or no root URL mapping |
| Board page blank / "Couldn't load the board" | `DISCORD_CLIENT_SECRET` missing/wrong, or `https://trades.example.com` not reachable — check the logs for "token exchange failed" |
| "You need to be a member of this server" | Viewer isn't in the server, or the bot isn't in it |
| Commands missing | Wait up to an hour, or set `GUILD_ID` and restart |
