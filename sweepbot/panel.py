"""Web panel of the sweep bot (DESIGN section 15).

The panel is a separate process, so a panel problem cannot stop the bot.
It reads two sources and writes one file:

* `state.json`: the live state, written by the bot every second
  (`Runner.write_state`).
* the SQLite store: today's trades, the decision log and the AI cost.
* `control.json`: the controls of a person (pause bids, sell all coins).
  The bot reads it every second (`Runner.apply_control`).

The server uses the standard library only. It listens on 127.0.0.1 by
default. On the shared server, open it through an SSH tunnel
(`ssh -L 8765:127.0.0.1:8765 root@server`), never on a public address:
the controls have no login.
"""

import http.server
import json
import logging
import os
import pathlib
import sqlite3
import time

from sweepbot import risk

log = logging.getLogger(__name__)

STATIC = pathlib.Path(__file__).resolve().parent / "panel_static" / "index.html"
# State older than this is stale: the bot is stopped or stuck.
STALE_S = 10.0
ACTIONS = {"pause": {"paused": True}, "resume": {"paused": False},
           "flatten": {"flatten": True}, "unflatten": {"flatten": False}}
LOG_ROWS = 60
TRADE_ROWS = 100


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    """Runs one read-only query. A missing database gives no rows."""
    if not os.path.exists(db_path):
        return []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def read_control(path: str) -> dict:
    """Returns the control file, or the default controls."""
    try:
        with open(path, encoding="utf-8") as control_file:
            control = json.load(control_file)
    except (OSError, ValueError):
        control = {}
    return {"paused": bool(control.get("paused")), "flatten": bool(control.get("flatten"))}


def write_control(path: str, action: str, now: float) -> dict:
    """Applies one panel action to the control file and returns the new controls.

    Raises:
        ValueError: An unknown action.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    control = read_control(path)
    control.update(ACTIONS[action])
    control["ts"] = now
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as control_file:
        json.dump(control, control_file)
    os.replace(tmp, path)
    return {"paused": control["paused"], "flatten": control["flatten"]}


def build_payload(*, state_path: str, db_path: str, control_path: str, now: float) -> dict:
    """Returns the JSON of `/api/state`: live state plus today's history."""
    try:
        with open(state_path, encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, ValueError):
        state = {}
    state_age = now - state["ts"] if "ts" in state else None
    mode = state.get("mode")
    day_start = now - (now + risk.TEHRAN_OFFSET_S) % 86400.0
    # All trades of the run (the challenge is short), newest first.
    all_trades = _rows(db_path, "SELECT ts, mode, market, entry_price, exit_price, net_pct,"
                                " net_tmn, hold_s, exit_reason FROM trades ORDER BY ts DESC")
    trades = all_trades[:TRADE_ROWS]
    today = [t for t in all_trades if t["ts"] >= day_start]
    logs = _rows(db_path, "SELECT ts, kind, text FROM logs ORDER BY ts DESC, id DESC LIMIT ?",
                 (LOG_ROWS,))
    ai = _rows(db_path, "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost"
                        " FROM ai_calls")
    bot_trades = [t for t in today if t["mode"] != "twin"]
    twin_trades = [t for t in today if t["mode"] == "twin"]

    def total(rows: list[dict]) -> dict:
        return {"trades": len(rows),
                "wins": sum(1 for t in rows if (t["net_tmn"] or 0.0) > 0),
                "net_tmn": sum(t["net_tmn"] or 0.0 for t in rows)}
    totals = {"bot": total([t for t in all_trades if t["mode"] != "twin"]),
              "twin": total([t for t in all_trades if t["mode"] == "twin"])}
    kpi = dict(state.get("kpi") or {})
    kpi.update({
        "net_today_bot": sum(t["net_tmn"] or 0.0 for t in bot_trades),
        "net_today_twin": sum(t["net_tmn"] or 0.0 for t in twin_trades),
        "trades_today": len(bot_trades),
        "wins_today": sum(1 for t in bot_trades if (t["net_tmn"] or 0.0) > 0),
        "ai_calls": ai[0]["calls"] if ai else 0,
        "ai_cost_usd": ai[0]["cost"] if ai else 0.0,
        "ai_budget_usd": (state.get("ai") or {}).get("budget_usd"),
    })
    return {
        "now": now, "mode": mode, "state_age_s": state_age,
        "stale": state_age is None or state_age > STALE_S,
        "control": read_control(control_path),
        "halted": state.get("halted"), "end": state.get("end") or {},
        "kpi": kpi, "slots": state.get("slots") or [], "health": state.get("health") or {},
        "wallet": state.get("wallet"), "funding": state.get("funding"),
        "allocation": state.get("allocation"), "twin": state.get("twin"),
        "totals": totals,
        "plan": state.get("plan"), "ai": state.get("ai") or {"enabled": False},
        "trades": trades, "logs": logs, "limits": state.get("limits") or {},
    }


def make_handler(*, state_path: str, db_path: str, control_path: str):
    """Returns the request handler class for these files."""

    class Handler(http.server.BaseHTTPRequestHandler):
        """Serves the page, the state and the controls."""

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path in ("/", "/index.html"):
                self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, build_payload(state_path=state_path, db_path=db_path,
                                              control_path=control_path, now=time.time()))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if self.path != "/api/control":
                self._json(404, {"error": "not found"})
                return
            # A browser sends a JSON body only with this header. A form on
            # another site cannot set it, so this blocks cross-site posts.
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self._json(415, {"error": "Content-Type must be application/json"})
                return
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                action = json.loads(self.rfile.read(length) or b"{}").get("action")
                control = write_control(control_path, action, time.time())
            except (ValueError, AttributeError) as error:
                self._json(400, {"error": str(error)})
                return
            log.warning("panel control: %s -> %s", action, control)
            self._json(200, {"control": control})

        def log_message(self, fmt: str, *args) -> None:
            log.debug("panel %s", fmt % args)

    return Handler


def serve(cfg: dict, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Runs the panel server until Ctrl+C."""
    out_dir = pathlib.Path(cfg["bot"]["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    handler = make_handler(state_path=str(out_dir / "state.json"),
                           db_path=cfg["bot"]["db_path"],
                           control_path=str(out_dir / "control.json"))
    server = http.server.ThreadingHTTPServer((host, port), handler)
    log.info("panel on http://%s:%d (bot files in %s)", host, port, out_dir)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
