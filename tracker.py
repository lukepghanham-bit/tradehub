"""Pure trade logic: parsing, R maths and candle-by-candle processing.

No Discord or network code lives here, so it can be tested on its own.

R is always measured against the ORIGINAL stop distance (1R = |entry - original stop|),
so moving the stop later doesn't change what "1R" means.
"""
import re
import time


class TradeError(ValueError):
    pass


# ---------------------------------------------------------------- parsing
def parse_price(text):
    t = (text or "").strip().lower().replace("$", "").replace(",", "")
    if t in ("", "market", "mkt", "m", "now"):
        return None
    try:
        v = float(t)
    except ValueError:
        raise TradeError(f"`{text}` isn't a number.")
    if v <= 0:
        raise TradeError("Prices must be above zero.")
    return v


def parse_targets(text):
    """Accepts one target per line (or comma / semicolon separated), e.g.
         17 50%        -> take 50% of the position at 17
         19.5          -> share of whatever % is left unassigned
         22 @ 25
    If no percentages are given, the position is split evenly across targets.
    If percentages add up to less than 100, the rest is a runner that rides
    until the stop is hit or the analyst closes it.
    """
    parts = [p.strip() for p in re.split(r"[\n;,]+", text or "") if p.strip()]
    if not parts:
        raise TradeError("Add at least one target.")
    if len(parts) > 6:
        raise TradeError("Max 6 targets.")
    out = []
    for p in parts:
        p = re.sub(r"^(t|tp)\s*\d+\s*[:\-=]?\s*", "", p, flags=re.I)  # strip "T1:" / "TP2 -"
        nums = re.findall(r"\d*\.?\d+", p.replace(",", "").replace("$", ""))
        if not nums:
            raise TradeError(f"Couldn't read target `{p}`.")
        price = float(nums[0])
        pct = float(nums[1]) if len(nums) > 1 else None
        out.append({"price": price, "pct": pct, "hit": False, "hit_at": None})
    given = sum(t["pct"] for t in out if t["pct"] is not None)
    if given > 100.0001:
        raise TradeError(f"Target percentages add up to {given:g}% (max 100%).")
    blanks = [t for t in out if t["pct"] is None]
    if blanks:
        share = (100 - given) / len(blanks)
        for t in blanks:
            t["pct"] = share
    return out


def validate(direction, entry, stop, targets):
    sign = 1 if direction == "long" else -1
    if (entry - stop) * sign <= 0:
        raise TradeError(f"For a {direction}, the stop must be {'below' if sign > 0 else 'above'} entry.")
    prev = entry
    for i, t in enumerate(targets, 1):
        if (t["price"] - prev) * sign <= 0:
            raise TradeError(
                f"T{i} ({t['price']:g}) must be {'above' if sign > 0 else 'below'} "
                f"{'entry' if i == 1 else f'T{i - 1}'} for a {direction}.")
        prev = t["price"]


# ---------------------------------------------------------------- maths
def sign(t):
    return 1 if t["direction"] == "long" else -1


def risk(t):
    return abs(t["entry"] - t["orig_stop"])


def r_at(t, price):
    return (price - t["entry"]) / risk(t) * sign(t)


def live_r(t, price=None):
    """Total R if everything still open were closed at `price` (realised + unrealised)."""
    if t["status"] == "closed":
        return t["result_r"] or 0.0
    if t["status"] != "open":
        return 0.0
    price = price if price is not None else t.get("last_price")
    if price is None:
        return t["banked"]
    return t["banked"] + t["remaining"] / 100 * r_at(t, price)


# ---------------------------------------------------------------- state changes
def _event(t, kind, text, **extra):
    e = {"kind": kind, "text": text, "at": time.time(), **extra}
    t.setdefault("events", []).append(e)
    return e


def _close(t, price, reason, now):
    t["banked"] += t["remaining"] / 100 * r_at(t, price)
    t["remaining"] = 0.0
    t["status"] = "closed"
    t["result_r"] = round(t["banked"], 2)
    t["close_reason"] = reason
    t["close_price"] = price
    t["closed_at"] = now


def process_candle(t, high, low, candle_ms=0, now=None, be_after_tp1=True):
    """Feed one price candle (high/low) into a trade. Returns a list of new events.

    If a single candle touches both the stop and a target we can't know which came
    first, so we assume the stop (the conservative read).

    `candle_ms` is the candle's open time. A stop set during a candle is only checked
    from the NEXT candle on (stop_guard_ms) — otherwise moving to breakeven after T1
    would get "hit" by a low that happened before the stop was moved."""
    now = now or time.time()
    s = sign(t)
    events = []

    if t["status"] == "pending":
        filled = low <= t["entry"] if s > 0 else high >= t["entry"]
        if not filled:
            return events
        t["status"] = "open"
        t["opened_at"] = now
        events.append(_event(t, "filled", f"Limit filled @ {fmt_price(t['entry'])}"))

    if t["status"] != "open":
        return events

    stop_live = candle_ms > (t.get("stop_guard_ms") or 0)
    stop_hit = stop_live and (low <= t["stop"] if s > 0 else high >= t["stop"])
    if stop_hit:
        stop_r = r_at(t, t["stop"])
        left = t["remaining"]
        moved = abs(t["stop"] - t["orig_stop"]) > 1e-12
        _close(t, t["stop"], "trailed stop" if moved else "stop", now)
        if moved:
            label = "breakeven" if abs(stop_r) < 0.05 else f"trailed stop @ {fmt_price(t['stop'])}"
            txt = f"Closed on {label} · {left:g}% @ {fmt_r(stop_r)}"
        else:
            txt = f"Stopped out @ {fmt_price(t['stop'])}"
        events.append(_event(t, "stop", txt + f" · total {fmt_r(t['result_r'])}"))
        return events

    for i, tg in enumerate(t["targets"], 1):
        if tg["hit"]:
            continue
        reached = high >= tg["price"] if s > 0 else low <= tg["price"]
        if not reached:
            break  # targets are in order; later ones can't be hit before this one
        tg["hit"] = True
        tg["hit_at"] = now
        take = min(tg["pct"], t["remaining"])
        tr = r_at(t, tg["price"])
        t["banked"] += take / 100 * tr
        t["remaining"] = max(0.0, t["remaining"] - take)
        txt = f"T{i} hit @ {fmt_price(tg['price'])} · took {take:g}% at {fmt_r(tr)}"
        if i == 1 and be_after_tp1 and t["remaining"] > 0 and (t["entry"] - t["stop"]) * s > 0:
            t["stop"] = t["entry"]
            t["stop_guard_ms"] = candle_ms
            txt += " · stop → breakeven"
        events.append(_event(t, "tp", txt, n=i))
        if t["remaining"] <= 1e-9:
            _close(t, tg["price"], "all targets", now)
            events.append(_event(t, "done", f"All targets hit · total {fmt_r(t['result_r'])}"))
            return events
    return events


def move_stop(t, price, now_ms=None):
    s = sign(t)
    ref = t.get("last_price")
    if ref is not None and (ref - price) * s <= 0:
        raise TradeError(f"Price is {fmt_price(ref)} — that stop would already be hit. Use /close instead.")
    old = t["stop"]
    t["stop"] = price
    now_ms = now_ms or int(time.time() * 1000)
    t["stop_guard_ms"] = now_ms // 60_000 * 60_000
    locks = t["banked"] + t["remaining"] / 100 * r_at(t, price)
    return _event(t, "move", f"Stop {fmt_price(old)} → {fmt_price(price)} (locks {fmt_r(locks)})")


def partial(t, price, pct):
    if t["status"] != "open":
        raise TradeError("Only open trades can be scaled out of.")
    take = min(pct, t["remaining"])
    if take <= 0:
        raise TradeError("Nothing left to take.")
    tr = r_at(t, price)
    t["banked"] += take / 100 * tr
    t["remaining"] -= take
    e = _event(t, "partial", f"Took {take:g}% @ {fmt_price(price)} ({fmt_r(tr)})")
    if t["remaining"] <= 1e-9:
        _close(t, price, "manual", time.time())
        _event(t, "done", f"Fully closed · total {fmt_r(t['result_r'])}")
    return e


def manual_close(t, price):
    if t["status"] == "pending":
        return cancel(t)
    left = t["remaining"]
    _close(t, price, "manual", time.time())
    return _event(t, "close", f"Closed {left:g}% @ {fmt_price(price)} · total {fmt_r(t['result_r'])}")


def cancel(t):
    t["status"] = "cancelled"
    t["closed_at"] = time.time()
    return _event(t, "cancel", "Cancelled — not counted in stats")


# ---------------------------------------------------------------- formatting
def fmt_price(p):
    if p is None:
        return "—"
    if p >= 1000:
        s = f"{p:,.2f}"
    elif p >= 1:
        s = f"{p:.4f}"
    else:
        # keep ~4 significant digits for tiny prices (0.00001234)
        import math
        decimals = min(12, max(4, -int(math.floor(math.log10(p))) + 3))
        s = f"{p:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "$" + s


def fmt_r(r):
    return f"{r:+.1f}R" if r is not None else "—"


def fmt_age(seconds):
    d, rem = divmod(int(max(0, seconds)), 86400)
    h = rem // 3600
    if d:
        return f"{d}d {h}h" if d < 3 else f"{d}d"
    m = (rem % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"
