"""SQLite store of the sweep bot: orders, fills, trades, plans and logs.

One process writes to the file (the bot). Each write commits at once, so a
crash loses little. The tables follow DESIGN section 10.
"""

import json
import os
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, ts REAL, market TEXT, side TEXT, price REAL,
    qty REAL, status TEXT, reason TEXT, plan_id INTEGER, mode TEXT);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts REAL,
    price REAL, qty REAL, fee REAL, maker INTEGER);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, market TEXT, mode TEXT,
    entry_fill_ids TEXT, exit_fill_ids TEXT, entry_price REAL,
    exit_price REAL, qty REAL, net_quote REAL, net_tmn REAL, net_pct REAL,
    hold_s REAL, exit_reason TEXT);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, provider TEXT,
    model TEXT, raw_json TEXT, validated_json TEXT, accepted INTEGER,
    notes TEXT);
CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, job TEXT, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    latency_s REAL, error TEXT);
CREATE TABLE IF NOT EXISTS filter_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, filter TEXT, key TEXT,
    kind TEXT);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, text TEXT,
    fields TEXT);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades (ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts);
"""

_TABLES = ("orders", "fills", "trades", "plans", "ai_calls", "filter_events",
           "logs")
_JSON_COLUMNS = ("entry_fill_ids", "exit_fill_ids", "fields")
_BOOL_COLUMNS = ("maker", "accepted")


class Store:
    """SQLite database with one write method for each table."""

    def __init__(self, path: str) -> None:
        """Opens the database and creates the tables when they are missing.

        Args:
            path: The database file, or ":memory:". The parent folder of a
                file is created. A file uses the WAL journal mode.
        """
        if path != ":memory:":
            folder = os.path.dirname(os.path.abspath(path))
            os.makedirs(folder, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _insert(self, sql: str, values: tuple) -> int:
        """Runs one insert, commits and returns the new row id."""
        cur = self._db.execute(sql, values)
        self._db.commit()
        return cur.lastrowid

    def add_order(self, *, order_id: str, ts: float, market: str, side: str,
                  price: float, qty: float, status: str, reason: str,
                  plan_id: int | None, mode: str) -> None:
        """Adds an order. `mode` is "live", "paper" or "twin"."""
        self._insert(
            "INSERT INTO orders (id, ts, market, side, price, qty, status,"
            " reason, plan_id, mode) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (order_id, ts, market, side, price, qty, status, reason, plan_id,
             mode))

    def set_order_status(self, order_id: str, status: str) -> None:
        """Sets the status of an order."""
        self._insert("UPDATE orders SET status = ? WHERE id = ?",
                     (status, order_id))

    def add_fill(self, *, order_id: str, ts: float, price: float, qty: float,
                 fee: float, maker: bool) -> int:
        """Adds a fill and returns its id."""
        return self._insert(
            "INSERT INTO fills (order_id, ts, price, qty, fee, maker)"
            " VALUES (?,?,?,?,?,?)",
            (order_id, ts, price, qty, fee, int(maker)))

    def add_trade(self, *, ts: float, market: str, mode: str,
                  entry_fill_ids: list[int], exit_fill_ids: list[int],
                  entry_price: float, exit_price: float, qty: float,
                  net_quote: float, net_tmn: float | None, net_pct: float,
                  hold_s: float, exit_reason: str) -> int:
        """Adds a closed trade and returns its id."""
        return self._insert(
            "INSERT INTO trades (ts, market, mode, entry_fill_ids,"
            " exit_fill_ids, entry_price, exit_price, qty, net_quote,"
            " net_tmn, net_pct, hold_s, exit_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, market, mode, json.dumps(entry_fill_ids),
             json.dumps(exit_fill_ids), entry_price, exit_price, qty,
             net_quote, net_tmn, net_pct, hold_s, exit_reason))

    def add_plan(self, *, ts: float, provider: str, model: str, raw_json: str,
                 validated_json: str | None, accepted: bool,
                 notes: str) -> int:
        """Adds a plan from the model and returns its id."""
        return self._insert(
            "INSERT INTO plans (ts, provider, model, raw_json,"
            " validated_json, accepted, notes) VALUES (?,?,?,?,?,?,?)",
            (ts, provider, model, raw_json, validated_json, int(accepted),
             notes))

    def add_ai_call(self, *, ts: float, job: str, model: str,
                    input_tokens: int, output_tokens: int, cost_usd: float,
                    latency_s: float, error: str | None) -> None:
        """Adds one model call with its cost."""
        self._insert(
            "INSERT INTO ai_calls (ts, job, model, input_tokens,"
            " output_tokens, cost_usd, latency_s, error)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts, job, model, input_tokens, output_tokens, cost_usd,
             latency_s, error))

    def add_filter_event(self, *, ts: float, filter_name: str, key: str,
                         kind: str) -> None:
        """Adds a filter event. `kind` is "start" or "end"."""
        self._insert(
            "INSERT INTO filter_events (ts, filter, key, kind)"
            " VALUES (?,?,?,?)", (ts, filter_name, key, kind))

    def add_log(self, *, ts: float, kind: str, text: str, fields: dict) -> int:
        """Adds a log row and returns its id.

        Args:
            ts: The time of the event.
            kind: "trade", "plan", "review", "risk" or "ai".
            text: The message.
            fields: Extra values. They are stored as JSON.
        """
        return self._insert(
            "INSERT INTO logs (ts, kind, text, fields) VALUES (?,?,?,?)",
            (ts, kind, text, json.dumps(fields)))

    def ai_cost_total(self) -> float:
        """Returns the sum of `cost_usd` of all model calls, 0.0 if none."""
        row = self._db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ai_calls").fetchone()
        return float(row[0])

    def rows(self, table: str, where: str = "",
             params: tuple = ()) -> list[dict]:
        """Returns the rows of a table in insert order.

        Args:
            table: One of the known table names.
            where: An SQL condition without the WHERE keyword.
            params: The values for the "?" marks of `where`.

        Returns:
            One dict for each row. JSON columns are decoded. The columns
            `maker` and `accepted` are bool.

        Raises:
            ValueError: If the table name is not known.
        """
        if table not in _TABLES:
            raise ValueError(f"unknown table: {table!r}")
        sql = f"SELECT * FROM {table}"
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY rowid"
        result = []
        for raw in self._db.execute(sql, params).fetchall():
            row = dict(raw)
            for name in _JSON_COLUMNS:
                if row.get(name) is not None:
                    row[name] = json.loads(row[name])
            for name in _BOOL_COLUMNS:
                if name in row:
                    row[name] = bool(row[name])
            result.append(row)
        return result

    def close(self) -> None:
        """Closes the database."""
        self._db.close()
