# Deploying Trade Hub on a VPS

Step-by-step setup for an Ubuntu/Debian VPS. Written so a person **or Claude Code running on the VPS** can follow it top to bottom.

The bot is installed in `/opt/tradehub/app` and runs as a `tradehub` system user. No domain, web server or open ports are needed — it only makes outgoing connections (Discord, Binance, Bybit, GitHub).

## Rules for whoever runs this

- **Never print, log, echo or ask for secrets in chat.** `DISCORD_TOKEN` is typed into `.env` by the owner. Create the file with an empty value and ask them to fill it in with `sudo -u tradehub nano /opt/tradehub/app/.env`.
- **Check each step before moving on** (each step lists a check). If a check fails, stop and fix it — don't skip ahead.
- **The VPS must not be in the US.** Binance and Bybit block US IPs, so prices won't load. Check with `curl -s https://ipinfo.io/country` (needs to print something other than `US`).
- Don't touch other services on the server.

## What only the owner can do

1. **Bot token:** Developer Portal → app → **Bot** → *Reset Token* → paste into `.env` as `DISCORD_TOKEN`.
2. **Deploy key:** add the server's public key to the GitHub repo (step 3 shows it).

---

## 1. Check the server

```bash
curl -s https://ipinfo.io/country; echo      # must NOT be US
python3 --version                            # needs 3.10+
lsb_release -ds 2>/dev/null || cat /etc/os-release | head -2
curl -sI https://data-api.binance.vision/api/v3/ping | head -1   # expect HTTP 200
```

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
Now **ask the owner** to open `.env` and paste their bot token:
```bash
sudo -u tradehub nano /opt/tradehub/app/.env
```
**Check (without revealing values):**
```bash
sudo -u tradehub bash -c 'cd /opt/tradehub/app && for k in DISCORD_TOKEN; do v=$(grep -E "^$k=" .env | cut -d= -f2-); if [ -z "$v" ] || [ "$v" = "paste-your-bot-token-here" ]; then echo "$k: MISSING"; else echo "$k: set"; fi; done'
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
sleep 8; sudo journalctl -u tradehub -n 30 --no-pager   # expect "Logged in as Trade Hub#…" and no tracebacks
```
If the log says "Improper token", the token in `.env` is wrong — ask the owner to reset it and paste again.

## 6. Discord setup (owner, in Discord)

1. Invite the bot if not done yet (OAuth2 → URL Generator, scopes `bot` + `applications.commands`).
2. Create channels: a hub feed (e.g. `#trade-hub`), the board (`#open-trades`), alerts (`#alerts`), one per analyst.
3. `/setup channels hub:#trade-hub board:#open-trades alerts:#alerts name:YourServerName`
4. `/analyst add channel:#pierre name:Pierre user:@Pierre` — once per analyst.
5. Lock `#open-trades` so only the bot can post. It then holds just the live board, refreshing every 5 seconds.

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
| Board not appearing | Run `/setup channels` again; check the bot can view/send in that channel |
| Logs show "board edit failed" with 429 | Discord rate limit — harmless, it retries on the next cycle |
| Commands missing | Wait up to an hour, or set `GUILD_ID` and restart |
