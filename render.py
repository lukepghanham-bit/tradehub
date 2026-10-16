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


# ---------------------------------------------------------------- the open-trades board
# Built with Discord's layout components (a container with text blocks and dividers),
# so the channel reads like a page rather than a chat message.

BOARD_TEXT_LIMIT = 3800  # Discord allows 4000 characters of text across the whole layout


def _targets_bar(t):
    hit = sum(1 for tg in t["targets"] if tg["hit"])
    return "▰" * hit + "▱" * (len(t["targets"]) - hit), hit


def _board_line(t, now):
    link = f"[{t['base']}]({t['_url']})" if t.get("_url") else f"**{t['base']}**"
    side = "Long" if t["direction"] == "long" else "Short"
    if t["status"] == "pending":
        return f"⏳ {link} {side} · limit {fmt_price(t['entry'])} · now {fmt_price(t.get('last_price'))} · {fmt_age(now - t['created_at'])}"
    r = live_r(t)
    bar, hit = _targets_bar(t)
    bits = [f"T{hit}/{len(t['targets'])}"] if hit else []
    if t["remaining"] < 99.99:
        bits.append(f"{t['remaining']:g}% left")
    if abs(t["stop"] - t["entry"]) < 1e-12:
        bits.append("🛡️ BE")
    bits.append(fmt_age(now - (t["opened_at"] or t["created_at"])))
    return f"{dot(r)} {link} {side} **`{fmt_r(r)}`** {bar} · " + " · ".join(bits)


def board_content(trades, hub_name="Trade Hub"):
    """Returns (blocks, colour). Each block is a markdown string shown as its own
    section, with dividers between them. Kept separate from Discord objects so it's testable."""
    now = time.time()
    live = [t for t in trades if t["status"] in ("open", "pending")]
    opened = [t for t in live if t["status"] == "open"]
    pending = [t for t in live if t["status"] == "pending"]
    total = sum(live_r(t) for t in opened)
    colour = BLUE if not opened else (GREEN if total > 0.05 else RED if total < -0.05 else GREY)

    head = f"## 📊 {hub_name} — Open Trades ({len(opened)})"
    if opened:
        up = sum(1 for t in opened if live_r(t) > 0.05)
        down = sum(1 for t in opened if live_r(t) < -0.05)
        head += f"\n**{fmt_r(total)}** live · 🟢 {up} up · 🔴 {down} down"
        if pending:
            head += f" · ⏳ {len(pending)} waiting"
    elif pending:
        head += f"\n⏳ {len(pending)} waiting for entry"
    else:
        head += "\nNo open trades right now. New calls show up here automatically."
    blocks = [head]

    by = defaultdict(list)
    for t in opened:
        by[t["analyst"]].append(t)
    for name in sorted(by, key=lambda n: -sum(live_r(t) for t in by[n])):
        rows = sorted(by[name], key=live_r, reverse=True)
        blocks.append(f"### {name} · {fmt_r(sum(live_r(t) for t in rows))}\n" + "\n".join(_board_line(t, now) for t in rows))
    if pending:
        blocks.append("### ⏳ Waiting for entry\n" + "\n".join(
            f"{_board_line(t, now)} · {t['analyst']}" for t in pending))

    # stay inside Discord's text limit: drop whole sections from the bottom, then say so
    footer_room = 120
    used, kept = 0, []
    for b in blocks:
        # also cap the number of sections: Discord allows 40 layout components per message
        if used + len(b) > BOARD_TEXT_LIMIT - footer_room or len(kept) >= 16:
            kept.append(f"*…and {len(blocks) - len(kept)} more sections — use /stats or the trade channels.*")
            break
        kept.append(b)
        used += len(b)
    return kept, colour


def board_view(trades, hub_name="Trade Hub"):
    blocks, colour = board_content(trades, hub_name)
    container = discord.ui.Container(accent_colour=colour)
    for i, block in enumerate(blocks):
        if i:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large if i == 1 else discord.SeparatorSpacing.small))
        container.add_item(discord.ui.TextDisplay(block))
    container.add_item(discord.ui.Separator(visible=False))
    container.add_item(discord.ui.TextDisplay(f"-# 🟢 Live · prices refresh every 5s · updated <t:{int(time.time())}:R>"))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


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
