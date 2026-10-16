"""Builds the Discord embeds: the per-trade card and the central open-trades board."""
import os
import time
from collections import defaultdict

import discord

from tracker import fmt_age, fmt_price, fmt_r, live_r, r_at, risk

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


def target_bar(t):
    """▰ for each target hit, ▱ for each still to go."""
    hit = sum(1 for tg in t["targets"] if tg["hit"])
    return "▰" * hit + "▱" * (len(t["targets"]) - hit), hit


def stop_distance_r(t):
    """How far price is from the stop, in R. None if unknown."""
    if t["status"] != "open" or t.get("last_price") is None:
        return None
    return (t["last_price"] - t["stop"]) * (1 if t["direction"] == "long" else -1) / risk(t)


def board_row(t, now):
    link = f"[{t['base']}]({t['_url']})" if t.get("_url") else f"**{t['base']}**"
    side = "Long" if t["direction"] == "long" else "Short"
    if t["status"] == "pending":
        return f"⏳ {link} {side} · limit {fmt_price(t['entry'])} · waiting {fmt_age(now - t['created_at'])}"
    r = live_r(t)
    bar, hit = target_bar(t)
    if t["remaining"] <= 0.01:
        progress = "all targets"
    elif t["remaining"] < 99.99:
        progress = f"{t['remaining']:g}% left"
    else:
        progress = f"T{hit}/{len(t['targets'])}" if hit else ""
    extras = [progress] if progress else []
    if abs(t["stop"] - t["entry"]) < 1e-12:
        extras.append("🛡️ BE")
    extras.append(fmt_age(now - (t["opened_at"] or t["created_at"])))
    return f"{dot(r)} {link} {side}  `{fmt_r(r)}`  {bar} · " + " · ".join(extras)


def board_embed(trades, hub_name="Trade Hub"):
    now = time.time()
    live = [t for t in trades if t["status"] in ("open", "pending")]
    opened = [t for t in live if t["status"] == "open"]
    pending = len(live) - len(opened)
    total = sum(live_r(t) for t in opened)

    colour = BLUE if not opened else (GREEN if total > 0.05 else RED if total < -0.05 else GREY)
    e = discord.Embed(title=f"📊 {hub_name.upper()} — OPEN TRADES", colour=colour)
    if not live:
        e.description = "*No open trades right now — new calls appear here automatically.*"
        e.set_footer(text="Live · updates every minute")
        e.timestamp = discord.utils.utcnow()
        return e

    green = sum(1 for t in opened if live_r(t) > 0.05)
    red = sum(1 for t in opened if live_r(t) < -0.05)
    summary = [f"**{len(opened)} open** · `{fmt_r(total)}` live · 🟢 {green} / 🔴 {red}"]
    if pending:
        summary[0] += f" · ⏳ {pending} pending"
    highlights = []
    if opened:
        best = max(opened, key=live_r)
        if live_r(best) > 0.05:
            highlights.append(f"🏆 Best: {best['analyst']} {best['base']} `{fmt_r(live_r(best))}`")
        risky = [(stop_distance_r(t), t) for t in opened if stop_distance_r(t) is not None]
        if risky:
            dist, t = min(risky, key=lambda x: x[0])
            if dist < 0.5:
                highlights.append(f"⚠️ Closest to stop: {t['analyst']} {t['base']} ({dist:.1f}R away)")
    e.description = "\n".join(summary + ([" · ".join(highlights)] if highlights else []))

    by = defaultdict(list)
    for t in live:
        by[t["analyst"]].append(t)

    def analyst_r(name):
        return sum(live_r(t) for t in by[name] if t["status"] == "open")

    for name in sorted(by, key=lambda n: -analyst_r(n))[:25]:
        group = sorted(by[name], key=lambda t: (t["status"] != "open", -live_r(t)))
        n_open = sum(1 for t in group if t["status"] == "open")
        header = f"{name} · {n_open} open"
        if n_open:
            header += f" · {fmt_r(analyst_r(name))}"
        rows, used = [], 0
        for i, t in enumerate(group):
            row = board_row(t, now)
            if used + len(row) + 1 > 980:  # leave room for "+N more"
                rows.append(f"*+{len(group) - i} more*")
                break
            rows.append(row)
            used += len(row) + 1
        e.add_field(name=header, value="\n".join(rows) + "\n\u200b", inline=False)

    e.set_footer(text="Live · updates every minute · tap a coin for the original call")
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
