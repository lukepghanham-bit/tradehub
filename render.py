"""Builds the Discord embeds: the per-trade card and the central open-trades board."""
import os
import time
from collections import defaultdict

import discord

from tracker import fmt_age, fmt_price, fmt_r, live_r, r_at

GREEN, RED, GREY, BLUE, AMBER = 0x57F287, 0xED4245, 0x99AAB5, 0x5865F2, 0xFEE75C

STATUS = {
    "pending": ("⏳ PENDING", AMBER),
    "open": ("🟢 LIVE", GREEN),
    "cancelled": ("⚪ CANCELLED", GREY),
}


def pair(t):
    return f"{t['base']}/{t['quote']}"


def dot(r):
    if r is None or abs(r) < 0.05:
        return "⚪"
    return "🟢" if r > 0 else "🔴"


def chart_name(t):
    return "chart" + (os.path.splitext(t["chart_path"])[1] or ".png")


def trade_embed(t, src_url=None, hub_name="Trade Hub"):
    now = time.time()
    if t["status"] == "closed":
        win = (t["result_r"] or 0) > 0.05
        label, colour = ("✅ CLOSED", GREEN) if win else (("➖ CLOSED", GREY) if (t["result_r"] or 0) > -0.05 else ("❌ STOPPED", RED))
    else:
        label, colour = STATUS[t["status"]]

    e = discord.Embed(title=f"{pair(t)} — {t['direction'].upper()}  {label}", colour=colour)
    e.set_author(name=f"{t['analyst']} · Analyst · #{t['id']}")

    lines = [f"**Entry** {fmt_price(t['entry'])}" + ("  *(limit)*" if t["status"] == "pending" else "")]
    stop_line = f"**Stop** {fmt_price(t['orig_stop'])}"
    if abs(t["stop"] - t["orig_stop"]) > 1e-12:
        locks = t["banked"] + t["remaining"] / 100 * r_at(t, t["stop"]) if t["status"] == "open" else None
        stop_line = f"**Stop** ~~{fmt_price(t['orig_stop'])}~~ → {fmt_price(t['stop'])}"
        if locks is not None:
            stop_line += f" (locks {fmt_r(locks)})"
    lines.append(stop_line)

    for i, tg in enumerate(t["targets"], 1):
        mark = " ✅" if tg["hit"] else ""
        lines.append(f"**T{i}** {fmt_price(tg['price'])} · {r_at(t, tg['price']):.1f}R · {tg['pct']:g}%{mark}")
    runner = 100 - sum(tg["pct"] for tg in t["targets"])
    if runner > 0.01:
        lines.append(f"**Runner** {runner:g}% rides until stop / manual close")

    if t.get("notes"):
        lines.append(f"\n📝 {t['notes']}")

    if t["status"] == "open":
        lr = live_r(t)
        lines.append(f"\n**Banked** {fmt_r(t['banked'])} · {t['remaining']:g}% running")
        if t.get("last_price"):
            lines.append(f"**Now** {fmt_price(t['last_price'])} · {dot(lr)} {fmt_r(lr)} total")
    elif t["status"] == "pending" and t.get("last_price"):
        lines.append(f"\n**Now** {fmt_price(t['last_price'])} · waiting for fill")
    elif t["status"] == "closed":
        held = (t["closed_at"] or now) - (t.get("opened_at") or t["created_at"])
        last = next((ev["text"] for ev in reversed(t["events"]) if ev["kind"] in ("stop", "close", "done")), "Closed")
        lines.append(f"\n🏁 {last} · held {fmt_age(held)}")
        lines.append(f"**Result {fmt_r(t['result_r'])}**")

    history = [ev for ev in t["events"] if ev["kind"] in ("tp", "move", "partial", "filled")]
    if history:
        lines.append("\n" + "\n".join(f"• <t:{int(ev['at'])}:d> {ev['text']}" for ev in history[-6:]))

    if src_url:
        lines.append(f"\n🔗 [View {t['analyst']}'s original post]({src_url})")

    e.description = "\n".join(lines)[:4000]
    if t.get("chart_path"):
        e.set_image(url="attachment://" + chart_name(t))
    e.set_footer(text=f"{hub_name} · from {t['analyst']}'s channel")
    e.timestamp = discord.utils.utcnow()
    return e


def board_embed(trades, hub_name="Trade Hub"):
    now = time.time()
    live = [t for t in trades if t["status"] in ("open", "pending")]
    e = discord.Embed(title=f"📊 {hub_name} — Open Trades ({len(live)})", colour=BLUE)
    if not live:
        e.description = "*No open trades right now.*"
    by = defaultdict(list)
    for t in live:
        by[t["analyst"]].append(t)
    total = 0.0
    blocks = []
    for name in sorted(by):
        rows = []
        for t in by[name]:
            d = t["direction"][0].upper()
            link = f"[{t['base']}]({t['_url']})" if t.get("_url") else t["base"]
            if t["status"] == "pending":
                rows.append(f"⏳ {link} {d} · limit {fmt_price(t['entry'])} · {fmt_age(now - t['created_at'])}")
                continue
            r = live_r(t)
            total += r
            left = f" · {t['remaining']:g}% left" if t["remaining"] < 99.99 else ""
            rows.append(f"{dot(r)} {link} {d} · {fmt_r(r)}{left} · {fmt_age(now - (t['opened_at'] or t['created_at']))}")
        blocks.append((name, "\n".join(rows)))

    for name, body in blocks[:25]:
        e.add_field(name=name, value=body[:1024], inline=False)
    if live:
        e.set_footer(text=f"Live · updates as trades move · open total {total:+.1f}R")
    else:
        e.set_footer(text="Live · updates as trades move")
    e.timestamp = discord.utils.utcnow()
    return e


def stats_embed(trades, title):
    e = discord.Embed(title=title, colour=BLUE)
    if not trades:
        e.description = "*No closed trades yet.*"
        return e
    by = defaultdict(list)
    for t in trades:
        by[t["analyst"]].append(t["result_r"] or 0)
    rows = []
    for name, rs in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        wins = sum(1 for r in rs if r > 0.05)
        rows.append(
            f"**{name}** · {fmt_r(sum(rs))} · {len(rs)} trades · {wins / len(rs):.0%} win · "
            f"avg {fmt_r(sum(rs) / len(rs))} · best {fmt_r(max(rs))}")
    allr = [t["result_r"] or 0 for t in trades]
    e.description = "\n".join(rows)
    e.set_footer(text=f"All analysts: {fmt_r(sum(allr))} over {len(allr)} trades")
    return e
