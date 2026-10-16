"""Web server for the Trade Board Discord Activity.

Discord loads the Activity page through its proxy (https://<app-id>.discordsays.com),
which forwards to this server. The page signs the viewer in with Discord, then reads
trades from the JSON API below. Only members of the server can see its trades.

Routes
  GET  /                      the Activity page (static files in web/static)
  GET  /api/config            public app id, so the page can start the SDK
  POST /api/token             swaps the OAuth code from the SDK for an access token
  GET  /api/board?guild_id=   open + pending trades for a server
  GET  /api/trades/{id}       one trade, with its full timeline
  GET  /api/trades/{id}/chart the trade's chart image
"""
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

import db
from tracker import live_r, r_at, risk

log = logging.getLogger("web")
STATIC = Path(__file__).with_name("web") / "static"
DISCORD_API = "https://discord.com/api/v10"
CACHE_TTL = 300  # seconds to trust a token / membership check

_token_user = {}  # access_token -> (user_id, expires)
_member_ok = {}   # (guild_id, user_id) -> (bool, expires)


# ---------------------------------------------------------------- auth
async def _user_for(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPUnauthorized(text="missing token")
    token = auth[7:]
    hit = _token_user.get(token)
    if hit and hit[1] > time.time():
        return hit[0]
    async with request.app["http"].get(f"{DISCORD_API}/users/@me",
                                       headers={"Authorization": f"Bearer {token}"}) as r:
        if r.status != 200:
            raise web.HTTPUnauthorized(text="bad token")
        uid = int((await r.json())["id"])
    _token_user[token] = (uid, time.time() + CACHE_TTL)
    return uid


async def _require_member(request, guild_id):
    uid = await _user_for(request)
    key = (guild_id, uid)
    hit = _member_ok.get(key)
    if hit and hit[1] > time.time():
        ok = hit[0]
    else:
        guild = request.app["bot"].get_guild(guild_id)
        ok = False
        if guild:
            try:
                ok = guild.get_member(uid) is not None or bool(await guild.fetch_member(uid))
            except Exception:
                ok = False
        _member_ok[key] = (ok, time.time() + CACHE_TTL)
    if not ok:
        raise web.HTTPForbidden(text="not a member of this server")
    return uid


# ---------------------------------------------------------------- serialising
def _trade_json(t, full=False):
    is_open = t["status"] == "open"
    lr = live_r(t)
    stop_dist = None
    if is_open and t.get("last_price") is not None:
        stop_dist = (t["last_price"] - t["stop"]) * (1 if t["direction"] == "long" else -1) / risk(t)
    out = {
        "id": t["id"], "analyst": t["analyst"], "base": t["base"], "quote": t["quote"],
        "direction": t["direction"], "status": t["status"],
        "entry": t["entry"], "stop": t["stop"], "orig_stop": t["orig_stop"],
        "targets": [{"price": tg["price"], "pct": tg["pct"], "hit": tg["hit"],
                     "r": round(r_at(t, tg["price"]), 2)} for tg in t["targets"]],
        "remaining": t["remaining"], "banked": round(t["banked"], 2),
        "live_r": round(lr, 2), "result_r": t["result_r"],
        "last_price": t.get("last_price"), "stop_dist_r": None if stop_dist is None else round(stop_dist, 2),
        "created_at": t["created_at"], "opened_at": t.get("opened_at"), "closed_at": t.get("closed_at"),
        "has_chart": bool(t.get("chart_path") and os.path.exists(t["chart_path"])),
        "src_url": (f"https://discord.com/channels/{t['guild_id']}/{t['channel_id']}/{t['src_msg_id']}"
                    if t.get("src_msg_id") else None),
    }
    if full:
        out["notes"] = t.get("notes")
        out["events"] = [{"kind": e["kind"], "text": e["text"], "at": e["at"]} for e in t["events"]]
    return out


# ---------------------------------------------------------------- handlers
async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def config(request):
    bot = request.app["bot"]
    return web.json_response({"client_id": str(bot.application_id or os.getenv("DISCORD_CLIENT_ID", ""))})


async def token(request):
    body = await request.json()
    code = body.get("code")
    secret = os.getenv("DISCORD_CLIENT_SECRET")
    if not code or not secret:
        raise web.HTTPBadRequest(text="missing code or DISCORD_CLIENT_SECRET")
    data = {"client_id": str(request.app["bot"].application_id), "client_secret": secret,
            "grant_type": "authorization_code", "code": code}
    async with request.app["http"].post(f"{DISCORD_API}/oauth2/token", data=data) as r:
        payload = await r.json()
        if r.status != 200:
            log.warning("token exchange failed: %s", payload)
            raise web.HTTPBadRequest(text="token exchange failed")
    return web.json_response({"access_token": payload["access_token"]})


async def board(request):
    try:
        gid = int(request.query["guild_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="guild_id required")
    await _require_member(request, gid)
    trades = [_trade_json(t) for t in db.active_trades(gid)]
    return web.json_response({
        "hub_name": db.get_setting(gid, "hub_name", "Trade Hub"),
        "trades": trades,
        "generated_at": time.time(),
    })


async def _load_trade(request):
    try:
        t = db.get_trade(int(request.match_info["tid"]))
    except ValueError:
        t = None
    if not t:
        raise web.HTTPNotFound()
    await _require_member(request, t["guild_id"])
    return t


async def trade(request):
    return web.json_response(_trade_json(await _load_trade(request), full=True))


async def chart(request):
    t = await _load_trade(request)
    if not t.get("chart_path") or not os.path.exists(t["chart_path"]):
        raise web.HTTPNotFound()
    return web.FileResponse(t["chart_path"], headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------- startup
async def start(bot, port):
    app = web.Application()
    app["bot"] = bot
    app["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    # Discord's Activity proxy may forward paths with or without a "/.proxy" prefix,
    # so every route is served under both.
    for pre in ("", "/.proxy"):
        app.router.add_get(pre + "/", index)
        app.router.add_get(pre + "/api/config", config)
        app.router.add_post(pre + "/api/token", token)
        app.router.add_get(pre + "/api/board", board)
        app.router.add_get(pre + "/api/trades/{tid}", trade)
        app.router.add_get(pre + "/api/trades/{tid}/chart", chart)
        app.router.add_static(pre + "/static/", STATIC)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1" if os.getenv("WEB_PUBLIC") != "1" else "0.0.0.0", port).start()
    log.info("Trade Board web server on port %s", port)
    return runner
