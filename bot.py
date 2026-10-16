"""Trade Hub — a Discord bot for analyst trade calls.

Analysts post trades from their own channel with /trade (a pop-up form with a chart
upload). The bot mirrors every call into a central hub channel, keeps one live
"Open Trades" board up to date, and watches live prices to mark targets and stops
automatically.
"""
import asyncio
import functools
import hashlib
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

import db
import prices
import render
import tracker
from tracker import TradeError, fmt_price, fmt_r

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tradehub")

CHART_DIR = Path(__file__).with_name("data") / "charts"
PRICE_EVERY = 5            # seconds between price checks
BOARD_EVERY = 5            # seconds between board refreshes (only edited when something changed)
BOARD_HEARTBEAT = 60       # edit at least this often so the 'updated' time stays fresh
CARD_REFRESH_EVERY = 300   # seconds between "live R" refreshes on trade cards
MAX_CHART_MB = 10

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
http: "prices.aiohttp.ClientSession | None" = None
state_lock = asyncio.Lock()  # stops the price loop and a command editing the same trade at once


# ====================================================================== helpers
def setting(gid, key, default=None):
    return db.get_setting(gid, key, default)


def hub_name(gid):
    return setting(gid, "hub_name", "Trade Hub")


def is_admin(inter: discord.Interaction):
    return inter.user.guild_permissions.manage_guild


def jump(gid, cid, mid):
    return f"https://discord.com/channels/{gid}/{cid}/{mid}" if mid else None


def parse_pair(text):
    s = re.sub(r"[^A-Za-z0-9/]", "", text or "").upper()
    if "/" in s:
        base, quote = s.split("/", 1)
    elif s.endswith(("USDT", "USDC")) and len(s) > 4:
        base, quote = s[:-4], s[-4:]
    else:
        base, quote = s, "USDT"
    if not base:
        raise TradeError("Enter a pair like `BTC` or `VVV/USDT`.")
    return base, quote or "USDT"


async def chart_file(t):
    if not t.get("chart_path") or not os.path.exists(t["chart_path"]):
        return None
    return discord.File(t["chart_path"], filename=render.chart_name(t))


async def get_channel(cid):
    if not cid:
        return None
    ch = client.get_channel(int(cid))
    if ch is None:
        try:
            ch = await client.fetch_channel(int(cid))
        except discord.HTTPException:
            return None
    return ch


async def refresh_cards(t):
    """Re-render the trade card in the analyst's channel and its mirror in the hub."""
    src_url = jump(t["guild_id"], t["channel_id"], t["src_msg_id"])
    name = hub_name(t["guild_id"])
    targets = [(t["channel_id"], t["src_msg_id"], None),
               (setting(t["guild_id"], "hub_channel"), t["hub_msg_id"], src_url)]
    for cid, mid, link in targets:
        ch = await get_channel(cid)
        if not ch or not mid:
            continue
        try:
            msg = ch.get_partial_message(int(mid))
            await msg.edit(embed=render.trade_embed(t, src_url=link, hub_name=name))
        except discord.HTTPException as e:
            log.warning("couldn't edit card for trade %s: %s", t["id"], e)


async def announce(t, events, ping=False):
    """Reply under the analyst's card and post to the alerts channel."""
    if not events:
        return
    text = "\n".join(f"{icon(e['kind'])} {e['text']}" for e in events)
    head = f"**{render.pair(t)} {t['direction'].title()}** · {t['analyst']}"
    src = await get_channel(t["channel_id"])
    if src and t["src_msg_id"]:
        try:
            await src.send(f"{head}\n{text}", reference=src.get_partial_message(t["src_msg_id"]),
                           mention_author=False, silent=True)
        except discord.HTTPException as e:
            log.warning("reply failed: %s", e)
    alerts = await get_channel(setting(t["guild_id"], "alerts_channel"))
    if alerts:
        role = alert_role(t)
        mention = f"<@&{role}> " if (ping and role) else ""
        url = jump(t["guild_id"], t["channel_id"], t["src_msg_id"])
        try:
            await alerts.send(f"{icon(events[-1]['kind'])} {mention}{head} — {text} · [view]({url})",
                              allowed_mentions=discord.AllowedMentions(roles=True), suppress_embeds=True)
        except discord.HTTPException as e:
            log.warning("alert failed: %s", e)


def alert_role(t):
    a = db.get_analyst(t["channel_id"])
    return (a and a.get("alert_role_id")) or setting(t["guild_id"], "alerts_role")


def icon(kind):
    return {"new": "🔔", "filled": "📥", "tp": "🎯", "move": "🛡️", "partial": "💰",
            "stop": "🏁", "close": "🏁", "done": "🏆", "cancel": "⚪"}.get(kind, "•")


def is_final(events):
    return any(e["kind"] in ("stop", "close", "done") for e in events)


# ====================================================================== the board
_board_lock = asyncio.Lock()


_board_last = {}  # guild_id -> (content hash, last edit time)


async def update_board(gid, force_new=False):
    """Keep the single board message in the board channel up to date.

    Called every 5 seconds; only edits the message when the numbers changed
    (or once a minute), so we stay well inside Discord's rate limits."""
    ch = await get_channel(setting(gid, "board_channel"))
    if not ch:
        return
    trades = db.active_trades(gid)
    for t in trades:
        t["_url"] = jump(gid, t["channel_id"], t["src_msg_id"])
    name = hub_name(gid)
    digest = hashlib.sha1(repr(render.board_content(trades, name)).encode()).hexdigest()
    last = _board_last.get(gid)
    if not force_new and last and last[0] == digest and time.time() - last[1] < BOARD_HEARTBEAT:
        return
    view = render.board_view(trades, name)
    async with _board_lock:
        mid = setting(gid, "board_message_v2")
        if mid and not force_new:
            try:
                await ch.get_partial_message(int(mid)).edit(view=view)
                _board_last[gid] = (digest, time.time())
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                log.warning("board edit failed: %s", e)
                return
        # first post (or re-post): remove the previous board message so the channel holds just one
        for key in ("board_message_v2", "board_message"):
            old = setting(gid, key)
            if old:
                try:
                    await ch.get_partial_message(int(old)).delete()
                except discord.HTTPException:
                    pass
                db.set_setting(gid, key, None)
        msg = await ch.send(view=view)
        db.set_setting(gid, "board_message_v2", msg.id)
        _board_last[gid] = (digest, time.time())


# ====================================================================== price loop
_last_card_refresh = defaultdict(float)


@tasks.loop(seconds=PRICE_EVERY)
async def price_loop():
    trades = db.active_trades()
    by_symbol = defaultdict(list)
    for t in trades:
        by_symbol[t["symbol"]].append(t)

    for symbol, group in by_symbol.items():
        start = min(t["last_check_ms"] for t in group)
        try:
            rows = await prices.candles(http, symbol, start)
        except Exception as e:  # never let one symbol kill the loop
            log.exception("price fetch %s: %s", symbol, e)
            continue
        if not rows:
            continue
        for stale in group:
            async with state_lock:
                t, events = step_trade(stale["id"], rows)
            if t is None:
                continue
            if events:
                await refresh_cards(t)
                await announce(t, events, ping=is_final(events))
                _last_card_refresh[t["id"]] = time.time()
            elif time.time() - _last_card_refresh[t["id"]] > CARD_REFRESH_EVERY:
                await refresh_cards(t)
                _last_card_refresh[t["id"]] = time.time()


def step_trade(tid, rows):
    """Run new candles through one trade and save it. Call with state_lock held."""
    t = db.get_trade(tid)  # re-read: a command may have changed it since the loop started
    if t["status"] not in ("open", "pending"):
        return None, []
    be = setting(t["guild_id"], "be_after_tp1", "1") == "1"
    events = []
    for open_ms, high, low, close in rows:
        if open_ms < t["last_check_ms"]:
            continue
        events += tracker.process_candle(t, high, low, open_ms, be_after_tp1=be)
        if t["status"] not in ("open", "pending"):
            break
    t["last_price"] = rows[-1][3]
    t["last_check_ms"] = rows[-1][0]  # the still-forming candle is re-checked next time
    db.save_trade(t)
    return t, events


@tasks.loop(seconds=BOARD_EVERY)
async def board_loop():
    for gid in db.guilds_with_settings():
        try:
            await update_board(gid)
        except Exception:
            log.exception("board update failed for %s", gid)


@price_loop.before_loop
@board_loop.before_loop
async def _wait_ready():
    await client.wait_until_ready()


# ====================================================================== /trade form
class TradeForm(discord.ui.Modal):
    def __init__(self, base, quote, direction, order):
        super().__init__(title=f"{base}/{quote} {direction.title()} ({order})"[:45])
        self.base, self.quote, self.direction, self.order = base, quote, direction, order
        entry_hint = "e.g. 14.19" if order == "limit" else "e.g. 14.19 — leave blank to use current price"
        self.entry = discord.ui.TextInput(placeholder=entry_hint, required=order == "limit", max_length=20)
        self.stop = discord.ui.TextInput(placeholder="e.g. 12", max_length=20)
        self.targets = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=300,
            placeholder="One per line, optional % to close:\n17 50%\n19.5 25%\n(25% left = runner)")
        self.notes = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, max_length=600,
            placeholder="Thesis, timeframe, bail signal…")
        self.chart = discord.ui.FileUpload(required=False, max_values=1)
        self.add_item(discord.ui.Label(text="Entry", component=self.entry))
        self.add_item(discord.ui.Label(text="Stop loss", component=self.stop))
        self.add_item(discord.ui.Label(text="Take-profit targets", component=self.targets))
        self.add_item(discord.ui.Label(text="Notes", component=self.notes))
        self.add_item(discord.ui.Label(text="Chart", description="Screenshot of your setup", component=self.chart))

    async def on_submit(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            await self._create(inter)
        except TradeError as e:
            await inter.followup.send(f"⚠️ {e}\nRun `/trade` again to retry.", ephemeral=True)

    async def _create(self, inter):
        analyst = db.get_analyst(inter.channel_id)
        symbol = f"{self.base}{self.quote}"
        now_price = await prices.last_price(http, symbol)
        if now_price is None:
            raise TradeError(f"Couldn't find **{symbol}** on Binance or Bybit. Check the ticker.")

        entry = tracker.parse_price(self.entry.value) or now_price
        stop = tracker.parse_price(self.stop.value)
        if stop is None:
            raise TradeError("Stop loss is required.")
        targets = tracker.parse_targets(self.targets.value)
        tracker.validate(self.direction, entry, stop, targets)
        sgn = 1 if self.direction == "long" else -1
        if self.order == "market" and (now_price - stop) * sgn <= 0:
            raise TradeError(f"Price is already {fmt_price(now_price)} — past your stop.")

        now = time.time()
        t = dict(
            guild_id=inter.guild_id, channel_id=inter.channel_id, analyst=analyst["name"],
            user_id=inter.user.id, symbol=symbol, base=self.base, quote=self.quote,
            direction=self.direction, entry=entry, stop=stop, orig_stop=stop, targets=targets,
            notes=(self.notes.value or "").strip() or None,
            status="pending" if self.order == "limit" else "open",
            remaining=100.0, banked=0.0, created_at=now,
            opened_at=None if self.order == "limit" else now,
            last_price=now_price, last_check_ms=int(now * 1000), events=[])
        t["id"] = db.insert_trade(t)
        tracker._event(t, "new", f"New {self.order} {self.direction} @ {fmt_price(entry)}")

        # chart
        if self.chart.values:
            att: discord.Attachment = self.chart.values[0]
            ext = os.path.splitext(att.filename)[1].lower() or ".png"
            if not (att.content_type or "").startswith("image/") or att.size > MAX_CHART_MB * 2**20:
                await inter.followup.send(f"Chart skipped — it must be an image under {MAX_CHART_MB} MB.", ephemeral=True)
            else:
                CHART_DIR.mkdir(parents=True, exist_ok=True)
                path = CHART_DIR / f"{t['id']}{ext}"
                path.write_bytes(await att.read())
                t["chart_path"] = str(path)

        name = hub_name(t["guild_id"])
        # 1) the card in the analyst's own channel
        src = await inter.channel.send(embed=render.trade_embed(t, hub_name=name), file=await chart_file(t) or discord.utils.MISSING)
        t["src_msg_id"] = src.id
        # 2) the mirror in the central hub channel
        hub = await get_channel(setting(t["guild_id"], "hub_channel"))
        if hub:
            m = await hub.send(embed=render.trade_embed(t, src_url=src.jump_url, hub_name=name),
                               file=await chart_file(t) or discord.utils.MISSING)
            t["hub_msg_id"] = m.id
        db.save_trade(t)

        # 3) ping
        alerts = await get_channel(setting(t["guild_id"], "alerts_channel"))
        if alerts:
            role = alert_role(t)
            mention = f"<@&{role}> " if role else ""
            await alerts.send(
                f"🔔 {mention}— new trade from **{t['analyst']}**: **{render.pair(t)} {self.direction.title()}** · [view]({src.jump_url})",
                allowed_mentions=discord.AllowedMentions(roles=True), suppress_embeds=True)

        await inter.followup.send(f"✅ Posted trade **#{t['id']}** — I'll track it from here.", ephemeral=True)
        await update_board(t["guild_id"])


@tree.command(description="Post a new trade call (opens a form)")
@app_commands.describe(pair="Ticker, e.g. BTC or VVV/USDT", direction="Long or short",
                       order="Market = you're in now. Limit = wait for price to reach entry.")
@app_commands.choices(direction=[app_commands.Choice(name="Long", value="long"),
                                 app_commands.Choice(name="Short", value="short")],
                      order=[app_commands.Choice(name="Market (in now)", value="market"),
                             app_commands.Choice(name="Limit (wait for entry)", value="limit")])
@app_commands.guild_only()
async def trade(inter: discord.Interaction, pair: str, direction: app_commands.Choice[str],
                order: app_commands.Choice[str] = None):
    analyst = db.get_analyst(inter.channel_id)
    if not analyst:
        return await inter.response.send_message(
            "This isn't an analyst channel. An admin can add it with `/analyst add`.", ephemeral=True)
    if analyst["user_id"] and analyst["user_id"] != inter.user.id and not is_admin(inter):
        return await inter.response.send_message(f"Only {analyst['name']} can post here.", ephemeral=True)
    try:
        base, quote = parse_pair(pair)
    except TradeError as e:
        return await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
    await inter.response.send_modal(TradeForm(base, quote, direction.value, order.value if order else "market"))


# ====================================================================== managing trades
async def trade_autocomplete(inter: discord.Interaction, current: str):
    out = []
    for t in db.active_trades(inter.guild_id):
        if t["user_id"] != inter.user.id and not is_admin(inter):
            continue
        label = f"#{t['id']} {t['base']} {t['direction']} · {t['analyst']} · {t['status']}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=t["id"]))
    return out[:25]


async def load_owned(inter, tid):
    t = db.get_trade(tid)
    if not t or t["guild_id"] != inter.guild_id:
        raise TradeError("Trade not found.")
    if t["user_id"] != inter.user.id and not is_admin(inter):
        raise TradeError("You can only manage your own trades.")
    if t["status"] not in ("open", "pending"):
        raise TradeError(f"Trade #{tid} is already {t['status']}.")
    return t


async def apply(inter, t, event, ping=False):
    db.save_trade(t)
    await refresh_cards(t)
    events = [event]
    if event["kind"] == "partial" and t["status"] == "closed":
        events.append(t["events"][-1])  # the "fully closed" summary
    await announce(t, events, ping=ping)
    await update_board(t["guild_id"])
    await inter.followup.send(f"✅ {event['text']}", ephemeral=True)


def managed(fn):
    """Wrap a trade-management command: defer, catch TradeError, report nicely."""
    @functools.wraps(fn)
    async def wrapper(inter: discord.Interaction, *args, **kwargs):
        await inter.response.defer(ephemeral=True)
        try:
            async with state_lock:
                await fn(inter, *args, **kwargs)
        except TradeError as e:
            await inter.followup.send(f"⚠️ {e}", ephemeral=True)
    return wrapper


@tree.command(name="move-stop", description="Move (trail) the stop on one of your trades")
@app_commands.autocomplete(trade=trade_autocomplete)
@app_commands.describe(trade="Which trade", price="New stop price (or 'be' for breakeven)")
@app_commands.guild_only()
@managed
async def move_stop_cmd(inter: discord.Interaction, trade: int, price: str):
    t = await load_owned(inter, trade)
    p = t["entry"] if price.strip().lower() in ("be", "breakeven", "entry") else tracker.parse_price(price)
    if p is None:
        raise TradeError("Give a price, or `be` for breakeven.")
    await apply(inter, t, tracker.move_stop(t, p))


@tree.command(name="take-profit", description="Manually scale out part of a trade")
@app_commands.autocomplete(trade=trade_autocomplete)
@app_commands.describe(trade="Which trade", percent="% of the ORIGINAL position to close",
                       price="Fill price (blank = current price)")
@app_commands.guild_only()
@managed
async def take_profit_cmd(inter: discord.Interaction, trade: int, percent: app_commands.Range[float, 1, 100],
                          price: str = None):
    t = await load_owned(inter, trade)
    p = tracker.parse_price(price) or await prices.last_price(http, t["symbol"])
    await apply(inter, t, tracker.partial(t, p, percent), ping=t["status"] == "closed")


@tree.command(name="close", description="Close the rest of a trade (or cancel a pending limit)")
@app_commands.autocomplete(trade=trade_autocomplete)
@app_commands.describe(trade="Which trade", price="Exit price (blank = current price)")
@app_commands.guild_only()
@managed
async def close_cmd(inter: discord.Interaction, trade: int, price: str = None):
    t = await load_owned(inter, trade)
    if t["status"] == "pending":
        return await apply(inter, t, tracker.cancel(t))
    p = tracker.parse_price(price) or await prices.last_price(http, t["symbol"])
    await apply(inter, t, tracker.manual_close(t, p), ping=True)


@tree.command(name="cancel", description="Void a trade posted by mistake (removed from stats)")
@app_commands.autocomplete(trade=trade_autocomplete)
@app_commands.guild_only()
@managed
async def cancel_cmd(inter: discord.Interaction, trade: int):
    t = await load_owned(inter, trade)
    await apply(inter, t, tracker.cancel(t))


@tree.command(name="update", description="Post an update/comment on one of your trades")
@app_commands.autocomplete(trade=trade_autocomplete)
@app_commands.guild_only()
@managed
async def update_cmd(inter: discord.Interaction, trade: int, text: app_commands.Range[str, 1, 500]):
    t = await load_owned(inter, trade)
    e = tracker._event(t, "note", f"💬 {text}")
    db.save_trade(t)
    await announce(t, [e])
    await inter.followup.send("✅ Update posted.", ephemeral=True)


# ====================================================================== stats
@tree.command(description="Leaderboard of closed trades")
@app_commands.describe(days="Only trades closed in the last N days (blank = all time)", analyst="Filter to one analyst")
@app_commands.guild_only()
async def stats(inter: discord.Interaction, days: app_commands.Range[int, 1, 3650] = None, analyst: str = None):
    since = time.time() - days * 86400 if days else None
    trades = db.closed_trades(inter.guild_id, analyst, since)
    title = f"🏆 {hub_name(inter.guild_id)} — " + (f"last {days}d" if days else "all time")
    await inter.response.send_message(embed=render.stats_embed(trades, title))


@stats.autocomplete("analyst")
async def _stats_ac(inter, current):
    return [app_commands.Choice(name=a["name"], value=a["name"])
            for a in db.list_analysts(inter.guild_id) if current.lower() in a["name"].lower()][:25]


# ====================================================================== admin setup
setup = app_commands.Group(name="setup", description="Configure the trade hub",
                           default_permissions=discord.Permissions(manage_guild=True), guild_only=True)


@setup.command(name="channels", description="Pick the hub, board and alerts channels")
@app_commands.describe(
    hub="Every trade card is mirrored here (the central feed)",
    board="Channel for the live Open Trades board",
    alerts="Channel for new-trade / TP / stop pings (optional)",
    alerts_role="Default role to ping for alerts (optional)",
    name="Name shown on the board, e.g. Haven")
async def setup_channels(inter: discord.Interaction, hub: discord.TextChannel, board: discord.TextChannel,
                         alerts: discord.TextChannel = None, alerts_role: discord.Role = None, name: str = None):
    g = inter.guild_id
    db.set_setting(g, "hub_channel", hub.id)
    db.set_setting(g, "board_channel", board.id)
    db.set_setting(g, "alerts_channel", alerts.id if alerts else None)
    db.set_setting(g, "alerts_role", alerts_role.id if alerts_role else None)
    if name:
        db.set_setting(g, "hub_name", name)
    await inter.response.send_message("✅ Saved. Posting the board…", ephemeral=True)
    await update_board(g, force_new=True)


@setup.command(name="breakeven", description="Auto-move the stop to entry after T1 hits (default: on)")
async def setup_be(inter: discord.Interaction, enabled: bool):
    db.set_setting(inter.guild_id, "be_after_tp1", "1" if enabled else "0")
    await inter.response.send_message(f"✅ Breakeven after T1: **{'on' if enabled else 'off'}**", ephemeral=True)


analyst_grp = app_commands.Group(name="analyst", description="Manage analysts",
                                 default_permissions=discord.Permissions(manage_guild=True), guild_only=True)


@analyst_grp.command(name="add", description="Make a channel an analyst's trade channel")
@app_commands.describe(channel="Their channel", name="Display name, e.g. Pierre",
                       user="Only this person can post there (optional)",
                       alert_role="Role to ping for this analyst's trades, e.g. @Alerts - Pierre (optional)")
async def analyst_add(inter: discord.Interaction, channel: discord.TextChannel, name: str,
                      user: discord.Member = None, alert_role: discord.Role = None):
    db.upsert_analyst(inter.guild_id, channel.id, name, user.id if user else None, alert_role.id if alert_role else None)
    await inter.response.send_message(f"✅ {channel.mention} is now **{name}**'s trade channel.", ephemeral=True)


@analyst_grp.command(name="remove", description="Stop using a channel as an analyst channel")
async def analyst_remove(inter: discord.Interaction, channel: discord.TextChannel):
    db.remove_analyst(channel.id)
    await inter.response.send_message(f"✅ Removed {channel.mention}. Its open trades keep being tracked.", ephemeral=True)


@analyst_grp.command(name="list", description="Show analyst channels")
async def analyst_list(inter: discord.Interaction):
    rows = db.list_analysts(inter.guild_id)
    body = "\n".join(f"• **{a['name']}** — <#{a['channel_id']}>" + (f" · <@{a['user_id']}>" if a["user_id"] else "")
                     + (f" · pings <@&{a['alert_role_id']}>" if a["alert_role_id"] else "") for a in rows)
    await inter.response.send_message(body or "No analysts yet — use `/analyst add`.", ephemeral=True)


tree.add_command(setup)
tree.add_command(analyst_grp)


# ====================================================================== startup
@client.event
async def on_ready():
    global http
    if http is None:
        http = prices.new_session()
        gid = os.getenv("GUILD_ID")
        if gid:  # instant command registration while testing in one server
            g = discord.Object(int(gid))
            tree.copy_global_to(guild=g)
            await tree.sync(guild=g)
        else:
            await tree.sync()
    if not price_loop.is_running():
        price_loop.start()
    if not board_loop.is_running():
        board_loop.start()
    log.info("Logged in as %s — tracking %d active trades", client.user, len(db.active_trades()))


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN (see README).")
    db.init()
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
