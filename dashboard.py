"""Authenticated monitoring dashboard and strategy administration panel."""

import hmac
import json
import math
import os
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, flash, jsonify, redirect, render_template, request, session, url_for,
)
import ccxt
import pandas as pd

import config
from logger_setup import logger
from signal_engine import SignalEngine
from state_manager import StateManager


app = Flask(__name__)
app.secret_key = config.DASHBOARD_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
state_manager = StateManager()
_login_attempts = {}
_chart_exchange = None
_chart_cache = {}
_chart_lock = threading.RLock()
CHART_TIMEFRAMES = ("15m", "1h", "4h")


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _market_chart(symbol, timeframe):
    """OHLCV publik USD-M; tidak memakai API key akun trading."""
    global _chart_exchange
    key = (symbol, timeframe)
    with _chart_lock:
        cached = _chart_cache.get(key)
        if cached and time.monotonic() - cached[0] < 10:
            return cached[1]
        if _chart_exchange is None:
            _chart_exchange = ccxt.binance({
                "enableRateLimit": True,
                "options": {"defaultType": "future", "fetchCurrencies": False},
            })
            if config.TRADING_MODE == "testnet":
                _chart_exchange.set_sandbox_mode(True)
        bars = _chart_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=240)
        if len(bars) < 55:
            raise ValueError("Data candle belum cukup untuk indikator")
        frame = pd.DataFrame(bars, columns=[
            "time", "open", "high", "low", "close", "volume",
        ])
        frame = SignalEngine(_chart_exchange).calculate_indicators(frame)
        candles = []
        for row in frame.tail(120).itertuples(index=False):
            candles.append({
                "time": int(row.time),
                "open": _finite(row.open), "high": _finite(row.high),
                "low": _finite(row.low), "close": _finite(row.close),
                "volume": _finite(row.volume),
                "ema21": _finite(row.ema_21), "ema55": _finite(row.ema_55),
                "ema200": _finite(row.ema_200),
                "rsi": _finite(row.rsi), "adx": _finite(row.adx),
                "atr": _finite(row.atr),
            })
        payload = {
            "symbol": symbol, "timeframe": timeframe,
            "source": "Binance USD-M public market data",
            "fetched_at": int(time.time() * 1000), "candles": candles,
        }
        _chart_cache[key] = (time.monotonic(), payload)
        if len(_chart_cache) > 12:
            oldest = min(_chart_cache, key=lambda item: _chart_cache[item][0])
            _chart_cache.pop(oldest, None)
        return payload


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _valid_csrf():
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    return bool(supplied and hmac.compare_digest(supplied, session.get("csrf_token", "")))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "authentication_required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.after_request
def security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' wss://fstream.binance.com wss://fstream.binancefuture.com"
    )
    return response


def _load_state():
    state_manager.state = state_manager._load_state(log=False)
    return state_manager.get_state()


def _performance_metrics(state):
    trades = state.get("trade_history") or []
    pnls = [float(trade.get("pnl") or 0) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    realized_r = [
        float(trade["realized_r"])
        for trade in trades if trade.get("realized_r") is not None
    ]
    return {
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "average_pnl": (sum(pnls) / len(pnls)) if pnls else 0,
        "average_r": (sum(realized_r) / len(realized_r)) if realized_r else None,
        "max_drawdown": max_drawdown,
        "consecutive_losses": int(state.get("consecutive_losses") or 0),
        "reconciled_trades": sum(t.get("pnl_status") == "reconciled" for t in trades),
        "estimated_trades": sum(t.get("pnl_status") != "reconciled" for t in trades),
    }


def _enrich_state(state):
    now = time.time()
    state["dashboard"] = {
        "server_time": datetime.now().isoformat(),
        "performance": _performance_metrics(state),
        "cooldown_remaining": max(0, int(float(state.get("cooldown_until") or 0) - now)),
        "mode": config.TRADING_MODE.upper(),
        "config": {
            "leverage": config.LEVERAGE,
            "margin_mode": config.MARGIN_MODE,
            "balance_usage_percent": config.BALANCE_USAGE * 100,
            "risk_per_trade_percent": config.RISK_PER_TRADE_PERCENT,
            "signal_min_score": config.SIGNAL_MIN_SCORE,
            "entry_strategy": config.ENTRY_STRATEGY,
            "scanner_top_n": config.SCANNER_TOP_N,
            "timeframe": config.TRADING_TIMEFRAME,
            "higher_timeframe": config.HIGHER_TIMEFRAME,
        },
    }
    return state


def _config_groups():
    groups = {}
    for name, meta in config.ADMIN_EDITABLE_CONFIG.items():
        item = {**meta, "name": name, "value": getattr(config, name)}
        groups.setdefault(meta["category"], []).append(item)
    return groups


def _persist_overrides():
    values = {name: getattr(config, name) for name in config.ADMIN_EDITABLE_CONFIG}
    target = os.path.abspath(config.RUNTIME_CONFIG_FILE)
    parent = os.path.dirname(target) or os.getcwd()
    os.makedirs(parent, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent,
            prefix=".strategy_overrides.", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(values, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    client = request.remote_addr or "unknown"
    now = time.time()
    attempts = [stamp for stamp in _login_attempts.get(client, []) if now - stamp < 300]
    _login_attempts[client] = attempts
    if request.method == "POST":
        if len(attempts) >= 5:
            flash("Terlalu banyak percobaan. Coba kembali dalam 5 menit.", "error")
            return render_template("login.html"), 429
        username_ok = hmac.compare_digest(request.form.get("username", ""), config.DASHBOARD_USERNAME)
        password_ok = hmac.compare_digest(request.form.get("password", ""), config.DASHBOARD_PASSWORD)
        if username_ok and password_ok:
            _login_attempts.pop(client, None)
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            session["username"] = config.DASHBOARD_USERNAME
            _csrf_token()
            next_url = request.args.get("next", "")
            safe_next = next_url.startswith("/") and not next_url.startswith("//")
            return redirect(next_url if safe_next else url_for("index"))
        attempts.append(now)
        _login_attempts[client] = attempts
        flash("Username atau password salah.", "error")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    if not _valid_csrf():
        return "Invalid CSRF token", 400
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "dashboard.html", username=session.get("username"), csrf_token=_csrf_token()
    )


@app.route("/markets")
@login_required
def markets():
    return render_template("markets.html", username=session.get("username"),
                           csrf_token=_csrf_token(), mode=config.TRADING_MODE)


@app.route("/admin")
@login_required
def admin():
    state = _load_state()
    return render_template(
        "admin.html", groups=_config_groups(), csrf_token=_csrf_token(),
        active_position=state.get("active_position"), mode=config.TRADING_MODE.upper(),
    )


@app.post("/admin/config")
@login_required
def update_config():
    if not _valid_csrf():
        return "Invalid CSRF token", 400
    submitted = {}
    for name in config.ADMIN_EDITABLE_CONFIG:
        values = request.form.getlist(name)
        if values:
            submitted[name] = values[-1]
    with config.RUNTIME_LOCK:
        config.load_runtime_overrides()
        previous = {name: getattr(config, name) for name in submitted}
        try:
            changed = config.apply_runtime_overrides(submitted)
            _persist_overrides()
            logger.warning("Dashboard strategy config saved by %s: %s", session.get("username"), sorted(changed))
            flash(f"{len(changed)} parameter disimpan; bot menerapkan pada siklus berikutnya.", "success")
        except (ValueError, TypeError, OSError) as exc:
            for name, value in previous.items():
                setattr(config, name, value)
            logger.error("Dashboard config update rejected: %s", exc)
            flash(str(exc), "error")
    return redirect(url_for("admin"))


@app.route("/api/state")
@login_required
def api_state():
    return jsonify(_enrich_state(_load_state()))


@app.route("/api/scan")
@login_required
def api_scan():
    state = _load_state()
    return jsonify({
        "scan": state.get("scan_monitor"),
        "bot_status": state.get("bot_status"),
        "connection_status": state.get("connection_status"),
        "cooldown_until": state.get("cooldown_until"),
        "active_position": state.get("active_position", {}).get("symbol")
            if state.get("active_position") else None,
        "server_time": time.time(),
        "min_score": config.SIGNAL_MIN_SCORE,
    })


@app.route("/api/market/chart")
@login_required
def api_market_chart():
    symbol = request.args.get("symbol", "")
    timeframe = request.args.get("timeframe", config.TRADING_TIMEFRAME)
    if timeframe not in CHART_TIMEFRAMES:
        return jsonify({"error": "invalid_timeframe"}), 400
    state = _load_state()
    scan = state.get("scan_monitor") or {}
    allowed = {row.get("symbol") for row in scan.get("markets", [])}
    allowed.add("BTC/USDT:USDT")
    for field in ("active_position", "pending_order", "last_signal"):
        value = state.get(field) or {}
        if value.get("symbol"):
            allowed.add(value["symbol"])
    if symbol not in allowed or not symbol.endswith("/USDT:USDT"):
        return jsonify({"error": "market_not_in_scanner"}), 400
    try:
        return jsonify(_market_chart(symbol, timeframe))
    except (ccxt.BaseError, ValueError, OSError) as exc:
        logger.warning("Dashboard candle fetch failed for %s %s: %s", symbol, timeframe, exc)
        return jsonify({"error": "market_data_unavailable"}), 503


@app.route("/api/config")
@login_required
def api_config():
    return jsonify({
        name: {**meta, "value": getattr(config, name)}
        for name, meta in config.ADMIN_EDITABLE_CONFIG.items()
    })


def run_dashboard():
    print(f"Dashboard running at http://localhost:{config.DASHBOARD_PORT}")
    try:
        from waitress import serve
        serve(
            app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
            threads=4, channel_timeout=30,
        )
    except ImportError:
        logger.warning("waitress belum terpasang; fallback ke Flask development server")
        app.run(
            host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT,
            debug=False, use_reloader=False,
        )


if __name__ == "__main__":
    run_dashboard()
