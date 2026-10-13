"""SQLite storage for trades, analysts and server settings."""
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("data") / "tradehub.db"


def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER, key TEXT, value TEXT,
                PRIMARY KEY (guild_id, key));
            CREATE TABLE IF NOT EXISTS analysts (
                channel_id INTEGER PRIMARY KEY, guild_id INTEGER,
                name TEXT, user_id INTEGER, alert_role_id INTEGER);
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, channel_id INTEGER, analyst TEXT, user_id INTEGER,
                symbol TEXT, base TEXT, quote TEXT, direction TEXT,
                entry REAL, stop REAL, orig_stop REAL,
                targets TEXT, notes TEXT,
                status TEXT, remaining REAL, banked REAL, result_r REAL,
                close_reason TEXT, close_price REAL,
                created_at REAL, opened_at REAL, closed_at REAL,
                last_price REAL, last_check_ms INTEGER, stop_guard_ms INTEGER,
                src_msg_id INTEGER, hub_msg_id INTEGER, chart_path TEXT,
                events TEXT);
            """
        )


# ---------- settings ----------
def get_setting(guild_id, key, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
    return r["value"] if r else default


def set_setting(guild_id, key, value):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (guild_id, key, None if value is None else str(value)))


def guilds_with_settings():
    with _conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT guild_id FROM settings")]


# ---------- analysts ----------
def upsert_analyst(guild_id, channel_id, name, user_id, alert_role_id):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO analysts VALUES (?,?,?,?,?)",
                  (channel_id, guild_id, name, user_id, alert_role_id))


def remove_analyst(channel_id):
    with _conn() as c:
        c.execute("DELETE FROM analysts WHERE channel_id=?", (channel_id,))


def get_analyst(channel_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM analysts WHERE channel_id=?", (channel_id,)).fetchone()
    return dict(r) if r else None


def list_analysts(guild_id):
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM analysts WHERE guild_id=? ORDER BY name", (guild_id,))]


# ---------- trades ----------
_JSON = ("targets", "events")


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in _JSON:
        d[k] = json.loads(d[k] or "[]")
    return d


def insert_trade(t):
    t = dict(t)
    t.setdefault("created_at", time.time())
    for k in _JSON:
        t[k] = json.dumps(t.get(k, []))
    cols = ",".join(t)
    with _conn() as c:
        cur = c.execute(f"INSERT INTO trades ({cols}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))
        return cur.lastrowid


def save_trade(t):
    d = dict(t)
    tid = d.pop("id")
    for k in _JSON:
        d[k] = json.dumps(d.get(k, []))
    with _conn() as c:
        c.execute(f"UPDATE trades SET {','.join(f'{k}=?' for k in d)} WHERE id=?", (*d.values(), tid))


def get_trade(tid):
    with _conn() as c:
        return _row(c.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone())


def active_trades(guild_id=None):
    q = "SELECT * FROM trades WHERE status IN ('pending','open')"
    args = ()
    if guild_id is not None:
        q += " AND guild_id=?"
        args = (guild_id,)
    with _conn() as c:
        return [_row(r) for r in c.execute(q + " ORDER BY created_at", args)]


def closed_trades(guild_id, analyst=None, since=None):
    q = "SELECT * FROM trades WHERE guild_id=? AND status='closed'"
    args = [guild_id]
    if analyst:
        q += " AND analyst=?"
        args.append(analyst)
    if since:
        q += " AND closed_at>=?"
        args.append(since)
    with _conn() as c:
        return [_row(r) for r in c.execute(q + " ORDER BY closed_at DESC", args)]
