"""A read-only local dashboard.

Deliberately built on ``http.server`` from the standard library: the spec allows
"a locally bound FastAPI application", but a dashboard is not worth a framework
dependency and Rule 8 asks for as few dependencies as possible. The server binds
to loopback by default and exposes exactly two routes:

    GET /            - static HTML shell
    GET /api/status  - JSON snapshot, same numbers as ``cryptobot status``

It is strictly read-only. There is no route that can submit an order, change the
system state, or reveal credentials: the dashboard is a window, not a control
panel. Controls live in the CLI, behind explicit operator commands.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from cryptobot.config.settings import Settings
from cryptobot.core.logging import get_logger
from cryptobot.journal.store import Journal
from cryptobot.sizing.ledger import AllocationLedger

log = get_logger(__name__)
UTC = timezone.utc


def _f(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return format(Decimal(str(value)), "f")


def status_payload(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    state, reason, updated = journal.get_system_state()
    allocation = ledger.state()
    balance = journal.latest_balance()
    health = journal.health()

    return {
        "generated_at": now.isoformat(),
        "app": {
            "name": settings.app.name,
            "environment": settings.app.environment,
            "paper_only": True,
        },
        "system": {
            "state": state.value,
            "reason": reason,
            "since": updated.isoformat() if updated else None,
        },
        "allocation": {
            "ceiling": _f(allocation.ceiling),
            "committed": _f(allocation.committed),
            "reserved": _f(allocation.reserved),
            "available": _f(allocation.available),
            "realized_pnl": _f(allocation.realized_pnl),
            "unrealized_pnl": _f(allocation.unrealized_pnl),
            "deployment_pct": _f(allocation.deployment_pct),
            "invariant_holds": allocation.invariant_holds,
        },
        "broker": (
            {
                "cash": _f(balance["cash"]),
                "equity": _f(balance["equity"]),
                "buying_power": _f(balance["buying_power"]),
                "as_of": balance["ts"],
            }
            if balance
            else None
        ),
        "positions": [
            {
                "symbol": p.symbol,
                "quantity": _f(p.quantity),
                "avg_entry_price": _f(p.avg_entry_price),
                "market_value": _f(p.market_value),
                "unrealized_pnl": _f(p.unrealized_pnl),
                "source": p.source,
            }
            for p in journal.latest_positions()
        ],
        "orders": [
            {
                "client_order_id": o.client_order_id,
                "symbol": o.symbol,
                "side": o.side.value,
                "quantity": _f(o.quantity),
                "filled_quantity": _f(o.filled_quantity),
                "status": o.status.value,
                "updated_at": o.updated_at.isoformat(),
            }
            for o in journal.list_orders(limit=20)
        ],
        "decisions": [
            {
                "ts": d["ts"],
                "symbol": d["symbol"],
                "action": d["action"],
                "reason": d["reason"],
                "strategy": d["strategy"],
            }
            for d in journal.recent_decisions(10)
        ],
        "reconciliation": [
            {
                "ts": r["ts"],
                "kind": r["kind"],
                "severity": r["severity"],
                "detail": r["detail_json"],
            }
            for r in journal.recent_reconciliations(5)
        ],
        "health": {k: v for k, v in health.items()},
        "limits": {
            "max_position_pct": _f(settings.risk.max_position_pct),
            "max_total_exposure_pct": _f(settings.risk.max_total_exposure_pct),
            "per_trade_risk_pct": _f(settings.risk.per_trade_risk_pct),
            "daily_loss_limit_pct": _f(settings.risk.daily_loss_limit_pct),
            "max_drawdown_pct": _f(settings.risk.max_drawdown_pct),
            "max_orders_per_day": settings.risk.max_orders_per_day,
            "symbol_allowlist": list(settings.risk.symbol_allowlist),
        },
    }


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CryptoBot status</title>
<style>
 :root { color-scheme: dark; }
 body { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace;
        background:#111418; color:#d7dde5; margin:0; padding:24px; }
 h1 { font-size:15px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:#8b98a8; }
 h2 { font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:#6d7b8d;
      margin:22px 0 6px; }
 table { border-collapse:collapse; width:100%; max-width:1100px; }
 td, th { text-align:left; padding:3px 14px 3px 0; white-space:nowrap; }
 th { color:#6d7b8d; font-weight:500; border-bottom:1px solid #222932; }
 .ok { color:#4ec9a0; } .bad { color:#e06c75; } .warn { color:#d7a94b; }
 .muted { color:#6d7b8d; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
         gap:2px 26px; max-width:1100px; }
 .k { color:#6d7b8d; } .num { text-align:right; font-variant-numeric:tabular-nums; }
</style>
</head>
<body>
<h1>CryptoBot &mdash; paper trading</h1>
<div id="top" class="muted">loading...</div>
<div id="body"></div>
<script>
function esc(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function pair(k, v, cls) {
  return '<div><span class="k">' + esc(k) + '</span> <span class="num ' + (cls||'') +
         '">' + esc(v) + '</span></div>';
}
function table(headers, rows) {
  if (!rows.length) return '<div class="muted">(none)</div>';
  var h = '<table><tr>' + headers.map(function(x){return '<th>'+esc(x)+'</th>';}).join('') + '</tr>';
  h += rows.map(function(r){
    return '<tr>' + r.map(function(c){return '<td>'+esc(c)+'</td>';}).join('') + '</tr>';
  }).join('');
  return h + '</table>';
}
function render(d) {
  var st = d.system.state;
  var cls = st === 'active' ? 'ok' : (st === 'killed' || st === 'halted' ? 'bad' : 'warn');
  document.getElementById('top').innerHTML =
    esc(d.app.name) + ' / ' + esc(d.app.environment) + ' &nbsp;&middot;&nbsp; state ' +
    '<span class="' + cls + '">' + esc(st) + '</span>' +
    (d.system.reason ? ' <span class="muted">(' + esc(d.system.reason) + ')</span>' : '') +
    ' &nbsp;&middot;&nbsp; ' + esc(d.generated_at);

  var a = d.allocation;
  var html = '<h2>Allocation</h2><div class="grid">' +
    pair('ceiling', a.ceiling) + pair('committed', a.committed) +
    pair('reserved', a.reserved) + pair('available', a.available) +
    pair('deployment %', a.deployment_pct) +
    pair('realised P&L', a.realized_pnl, a.realized_pnl.indexOf('-') === 0 ? 'bad' : 'ok') +
    pair('unrealised P&L', a.unrealized_pnl) +
    pair('invariant', a.invariant_holds ? 'OK' : 'VIOLATED', a.invariant_holds ? 'ok' : 'bad') +
    '</div>';

  if (d.broker) {
    html += '<h2>Broker account</h2><div class="grid">' +
      pair('cash', d.broker.cash) + pair('equity', d.broker.equity) +
      pair('buying power', d.broker.buying_power) + pair('as of', d.broker.as_of) + '</div>';
  }

  html += '<h2>Positions</h2>' + table(
    ['symbol','quantity','avg entry','market value','unrealised','source'],
    d.positions.map(function(p){return [p.symbol,p.quantity,p.avg_entry_price,p.market_value,p.unrealized_pnl,p.source];}));

  html += '<h2>Orders</h2>' + table(
    ['client order id','symbol','side','qty','filled','status','updated'],
    d.orders.map(function(o){return [o.client_order_id,o.symbol,o.side,o.quantity,o.filled_quantity,o.status,o.updated_at];}));

  html += '<h2>Recent decisions</h2>' + table(
    ['time','symbol','action','reason','strategy'],
    d.decisions.map(function(x){return [x.ts,x.symbol,x.action,x.reason,x.strategy];}));

  html += '<h2>Reconciliation</h2>' + table(
    ['time','severity','kind','detail'],
    d.reconciliation.map(function(r){return [r.ts,r.severity,r.kind,r.detail];}));

  html += '<h2>Risk limits</h2><div class="grid">' +
    pair('max position %', d.limits.max_position_pct) +
    pair('max exposure %', d.limits.max_total_exposure_pct) +
    pair('per-trade risk %', d.limits.per_trade_risk_pct) +
    pair('daily loss limit %', d.limits.daily_loss_limit_pct) +
    pair('max drawdown %', d.limits.max_drawdown_pct) +
    pair('max orders/day', d.limits.max_orders_per_day) +
    pair('allowlist', d.limits.symbol_allowlist.join(', ')) + '</div>';

  html += '<h2>Journal counters</h2><div class="grid">' +
    Object.keys(d.health).map(function(k){return pair(k, d.health[k]);}).join('') + '</div>';
  document.getElementById('body').innerHTML = html;
}
function tick() {
  fetch('api/status', {cache:'no-store'})
    .then(function(r){ return r.json(); })
    .then(render)
    .catch(function(e){
      document.getElementById('top').textContent = 'status unavailable: ' + e;
    });
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _Handler)
        self.status_provider: Callable[[], dict[str, Any]] | None = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "cryptobot-dashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif route == "/api/status":
            provider = getattr(self.server, "status_provider", None)
            if provider is None:
                self._send(503, "text/plain; charset=utf-8", b"no status provider\n")
                return
            payload = provider()
            self._send(
                200,
                "application/json; charset=utf-8",
                json.dumps(payload, default=str).encode("utf-8"),
            )
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        log.debug("dashboard request", extra={"context": {"line": fmt % args}})


def make_server(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    *,
    host: str | None = None,
    port: int | None = None,
) -> ThreadingHTTPServer:
    host = host or settings.monitoring.dashboard_host
    port = int(port if port is not None else settings.monitoring.dashboard_port)
    server = _Server((host, port))
    server.status_provider = lambda: status_payload(settings, journal, ledger)
    return server


class DashboardServer:
    """Background server so the dashboard can run alongside the trading loop."""

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> "DashboardServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="cryptobot-dashboard", daemon=True
        )
        self._thread.start()
        log.info("dashboard listening on %s", self.url)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "DashboardServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def serve_dashboard(
    settings: Settings,
    journal: Journal,
    ledger: AllocationLedger,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    server = make_server(settings, journal, ledger, host=host, port=port)
    bind_host, bind_port = server.server_address[:2]
    log.info("dashboard listening on http://%s:%s", bind_host, bind_port)
    print(f"dashboard: http://{bind_host}:{bind_port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        server.server_close()
