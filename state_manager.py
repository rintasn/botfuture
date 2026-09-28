"""
State Manager
=============
Menyimpan dan memulihkan state bot ke/dari JSON file.
Memungkinkan recovery setelah restart tanpa kehilangan informasi posisi.
"""

import os
import json
import time
import copy
import tempfile
import threading
from datetime import datetime, timedelta
from logger_setup import logger
from runtime_config import config


class StateManager:
    """Mengelola state persistence bot ke file JSON."""
    
    def __init__(self, state_file=None):
        self.state_file = state_file or config.STATE_FILE
        self._lock = threading.RLock()
        self.state = self._load_state()
    
    def _default_state(self):
        """State default saat pertama kali dijalankan."""
        return {
            "bot_status": "idle",               # idle, scanning, trading, monitoring, cooldown
            "active_position": None,             # Info posisi aktif
            "pending_order": None,               # Info pending limit order
            "trailing_stop": None,               # Info trailing stop aktif
            "current_checkpoint": 0,             # Checkpoint trailing saat ini
            "last_signal": None,                 # Signal terakhir yang terdeteksi
            "last_scan_time": None,              # Waktu scan terakhir
            "scan_monitor": None,                # Snapshot scanner untuk dashboard
            "trade_history": [],                 # Riwayat trade
            "total_trades": 0,
            "total_profit": 0.0,
            "consecutive_losses": 0,             # Jumlah loss beruntun
            "cooldown_until": 0,                 # Timestamp berakhirnya cooldown global
            "symbol_cooldowns": {},              # Timestamp cooldown per simbol {symbol: timestamp}
            "start_time": datetime.now().isoformat(),
            "protection_status": "none",       # none, pending, protected, missing, unknown, failed
            "connection_status": "starting",  # starting, connected, degraded, api_error
            "last_api_error": None,
            "last_reconciliation": None,
            "last_websocket_event": None,
            "risk_circuit_last_trigger_trade": 0,
            "exit_intent": None,
        }
    
    def _load_state(self, log=True):
        """Load state dari file JSON."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                
                # Pastikan key baru ada (backward compatibility)
                default = self._default_state()
                for k, v in default.items():
                    if k not in state:
                        state[k] = v
                pos = state.get("active_position")
                if pos:
                    plan = pos.get("initial_stop_plan") or {}
                    pos.setdefault("initial_amount", pos.get("amount", 0))
                    pos.setdefault("lowest_profit_pct", 0.0)
                    pos.setdefault("initial_stop_price", plan.get("stop_price"))
                    pos.setdefault("initial_risk_pct", float(plan.get("distance_pct") or 0))
                    stop_price = float(pos.get("initial_stop_price") or pos.get("entry_price") or 0)
                    pos.setdefault(
                        "initial_risk_usdt",
                        float(pos.get("initial_amount") or 0)
                        * abs(float(pos.get("entry_price") or 0) - stop_price),
                    )
                    pos.setdefault("tp1_done", False)
                    pos.setdefault("tp2_done", False)
                    pos.setdefault("defensive_reduction_done", False)
                    pos.setdefault("partial_exits", [])
                    pos.setdefault("realized_partial_pnl", 0.0)
                    pos.setdefault("entry_signal_snapshot", None)
                
                if log:
                    logger.info(f"📂 State loaded dari {self.state_file}")
                return state
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"⚠️ Gagal load state: {e}. Menggunakan default.")
        
        return self._default_state()
    
    def save(self, strict=False):
        """Simpan state secara atomic agar JSON tidak pernah terbaca setengah."""
        temp_path = None
        try:
            with self._lock:
                target = os.path.abspath(self.state_file)
                parent = os.path.dirname(target) or os.getcwd()
                os.makedirs(parent, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=parent,
                    prefix=f".{os.path.basename(target)}.",
                    suffix=".tmp",
                    delete=False,
                ) as tmp:
                    temp_path = tmp.name
                    json.dump(self.state, tmp, indent=2, default=str)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(temp_path, target)
                temp_path = None
        except (IOError, OSError) as e:
            logger.error(f"❌ Gagal simpan state: {e}")
            if strict:
                raise
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    
    # =========================================================================
    # Cooldown & Protection Management
    # =========================================================================
    
    def set_cooldown(self, symbol=None, minutes=None, reason="", ignore_loss_multiplier=False):
        """
        Aktifkan cooldown global dan/atau per-symbol.
        
        Args:
            symbol: (Opsional) Symbol spesifik yang di-cooldown
            minutes: Durasi dalam menit (default: config.SIGNAL_COOLDOWN_MINUTES)
            reason: Alasan cooldown
            ignore_loss_multiplier: Abaikan pengali consecutive loss (misal untuk timeout order biasa)
        """
        now = time.time()
        
        # Hitung durasi cooldown global (cek consecutive loss)
        losses = self.state.get("consecutive_losses", 0)
        base_minutes = minutes if minutes is not None else config.SIGNAL_COOLDOWN_MINUTES
        
        if not ignore_loss_multiplier and losses >= config.MAX_CONSECUTIVE_LOSSES:
            actual_minutes = getattr(config, "CONSECUTIVE_LOSS_PAUSE_MINUTES", 120)
            logger.warning(
                f"🛑 Max Consecutive Losses ({losses}) tercapai! "
                f"Cooldown losestreak diaktifkan: {actual_minutes} menit sebelum auto-resume."
            )
        elif not ignore_loss_multiplier and losses >= config.DOUBLE_COOLDOWN_AFTER_LOSSES:
            actual_minutes = base_minutes * 2
            logger.warning(
                f"⚠️ Consecutive losses = {losses}! "
                f"Double cooldown diaktifkan: {actual_minutes} menit."
            )
        else:
            actual_minutes = base_minutes
            
        expire_time = now + (actual_minutes * 60)
        self.state["cooldown_until"] = expire_time
        
        # Set cooldown spesifik symbol jika ada
        if symbol:
            sym_minutes = config.SYMBOL_COOLDOWN_MINUTES
            self.state["symbol_cooldowns"][symbol] = now + (sym_minutes * 60)
            logger.info(
                f"⏳ Cooldown {symbol} aktif selama {sym_minutes}m. Reason: {reason}"
            )
            
        logger.info(
            f"⏳ Global cooldown aktif selama {actual_minutes}m "
            f"(hingga {datetime.fromtimestamp(expire_time).strftime('%H:%M:%S')}). Reason: {reason}"
        )
        self.save()

    def is_cooldown_active(self, symbol=None):
        """
        Cek apakah saat ini sedang dalam masa cooldown.
        
        Returns:
            tuple: (is_active: bool, remaining_seconds: float, reason: str)
        """
        now = time.time()
        
        # 1. Cek Global Cooldown (termasuk pause losestreak)
        global_until = self.state.get("cooldown_until", 0)
        if now < global_until:
            rem = global_until - now
            losses = self.state.get("consecutive_losses", 0)
            if losses >= config.MAX_CONSECUTIVE_LOSSES:
                return True, rem, f"Losestreak pause ({int(rem/60)}m tersisa)"
            return True, rem, f"Global cooldown ({int(rem)}s tersisa)"
        else:
            # Jika masa cooldown losestreak sudah lewat, auto-reset counter.
            if self.state.get("consecutive_losses", 0) >= config.MAX_CONSECUTIVE_LOSSES:
                logger.info("🔄 Cooldown losestreak 2 jam selesai. Auto-reset consecutive losses ke 0.")
                self.state["consecutive_losses"] = 0
                self.save()
            
        # 2. Cek Symbol Cooldown
        if symbol:
            sym_until = self.state.get("symbol_cooldowns", {}).get(symbol, 0)
            if now < sym_until:
                rem = sym_until - now
                return True, rem, f"Symbol cooldown untuk {symbol} ({int(rem)}s tersisa)"
                
        return False, 0, ""

    def reset_consecutive_losses(self):
        """Reset hitungan loss beruntun (misal setelah win atau manual reset)."""
        self.state["consecutive_losses"] = 0
        self.save()
        logger.info("🔄 Consecutive losses counter di-reset ke 0.")

    # =========================================================================
    # Position Management
    # =========================================================================
    
    def set_position(
        self, symbol, side, entry_price, amount, order_id, entry_time=None,
        initial_stop_plan=None, entry_signal_snapshot=None,
    ):
        """Simpan info posisi aktif."""
        self.state["active_position"] = {
            "symbol": symbol,
            "side": side,                    # 'long' atau 'short'
            "entry_price": entry_price,
            "amount": amount,
            "initial_amount": amount,
            "order_id": order_id,
            "entry_time": entry_time or datetime.now().isoformat(),
            "highest_profit_pct": 0.0,
            "lowest_profit_pct": 0.0,
            "initial_stop_plan": copy.deepcopy(initial_stop_plan),
            "initial_stop_price": (initial_stop_plan or {}).get("stop_price"),
            "initial_risk_pct": float((initial_stop_plan or {}).get("distance_pct") or 0),
            "initial_risk_usdt": float(amount) * abs(
                float(entry_price) - float((initial_stop_plan or {}).get("stop_price") or entry_price)
            ),
            "tp1_done": False,
            "tp2_done": False,
            "defensive_reduction_done": False,
            "partial_exits": [],
            "realized_partial_pnl": 0.0,
            "entry_signal_snapshot": copy.deepcopy(entry_signal_snapshot),
        }
        self.state["trailing_stop"] = None
        self.state["current_checkpoint"] = 0
        self.state["bot_status"] = "monitoring"
        self.state["protection_status"] = "pending"
        self.save()
        logger.info(f"📊 Position saved: {side.upper()} {symbol} @ {entry_price}")
    
    def clear_position(self, pnl=0.0, reason="", start_cooldown=True, record_history=True):
        """Hapus posisi aktif, catat ke history, dan update statistik."""
        pos = self.state["active_position"]
        symbol = pos["symbol"] if pos else None
        
        if pos and record_history:
            pnl = float(pnl) + float(pos.get("realized_partial_pnl", 0) or 0)
            initial_risk = float(pos.get("initial_risk_usdt", 0) or 0)
            trade_record = {
                **pos,
                "close_time": datetime.now().isoformat(),
                "pnl": pnl,
                "realized_r": (pnl / initial_risk) if initial_risk > 0 else None,
                "close_reason": reason,
                "pnl_status": "estimated",
            }
            self.state["trade_history"].append(trade_record)
            self.state["total_trades"] += 1
            self.state["total_profit"] += pnl
            
            # Update consecutive losses
            if pnl < 0:
                self.state["consecutive_losses"] = self.state.get("consecutive_losses", 0) + 1
                logger.warning(
                    f"🔻 Trade Loss recorded: {pnl:.2f} USDT | "
                    f"Consecutive Losses: {self.state['consecutive_losses']}"
                )
            elif initial_risk > 0 and pnl / initial_risk < getattr(config, "MEANINGFUL_WIN_R", 0.25):
                logger.info(f"Trade kecil {pnl:+.2f} USDT tidak mereset loss streak.")
            else:
                self.state["consecutive_losses"] = 0
                logger.info(f"✨ Trade Win/Breakeven recorded: {pnl:.2f} USDT")
        
        self.state["active_position"] = None
        self.state["trailing_stop"] = None
        self.state["current_checkpoint"] = 0
        self.state["protection_status"] = "none"
        self.state["bot_status"] = "idle"
        self.save()
        logger.info(f"🔄 Position cleared. Reason: {reason}, PnL: {pnl:+.2f} USDT")
        
        if start_cooldown:
            self.set_cooldown(symbol=symbol, reason=f"Position closed ({reason})")
    
    def get_position(self):
        """Ambil info posisi aktif."""
        return self.state["active_position"]
    
    def has_position(self):
        """Cek apakah ada posisi aktif."""
        return self.state["active_position"] is not None
    
    # =========================================================================
    # Pending Order Management
    # =========================================================================
    
    def set_pending_order(
        self, symbol, side, price, amount, order_id, timeout_minutes=None,
        client_order_id=None, good_till_date=None, status_unknown=False,
        initial_stop_plan=None, entry_signal_snapshot=None,
    ):
        """Simpan info pending limit order."""
        if timeout_minutes is None:
            timeout_minutes = config.ORDER_TIMEOUT_MINUTES

        self.state["pending_order"] = {
            "symbol": symbol,
            "side": side,
            "price": price,
            "amount": amount,
            "order_id": order_id,
            "placed_time": time.time(),
            "placed_time_str": datetime.now().isoformat(),
            "timeout_minutes": timeout_minutes,
            "client_order_id": client_order_id,
            "good_till_date": good_till_date,
            "status_unknown": bool(status_unknown),
            "last_deadman_refresh": 0,
            "initial_stop_plan": copy.deepcopy(initial_stop_plan),
            "entry_signal_snapshot": copy.deepcopy(entry_signal_snapshot),
            "last_signal_validation": 0,
        }
        self.state["bot_status"] = "waiting_fill"
        self.save()
        logger.info(
            f"📝 Pending order saved: {side.upper()} {symbol} @ {price} "
            f"(Timeout: {timeout_minutes}m)"
        )
    
    def clear_pending_order(self, start_cooldown=False, symbol=None):
        """Hapus pending order."""
        self.state["pending_order"] = None
        self.state["bot_status"] = "monitoring" if self.state.get("active_position") else "idle"
        self.save()
        if start_cooldown:
            # Cooldown ringan jika order timeout/cancel (hanya jeda singkat sebelum scan koin lain)
            timeout_cooldown = getattr(config, "TIMEOUT_COOLDOWN_MINUTES", 1)
            self.set_cooldown(
                symbol=symbol,
                minutes=timeout_cooldown,
                reason="Pending order cancelled/timeout",
                ignore_loss_multiplier=True
            )
    
    def get_pending_order(self):
        """Ambil info pending order."""
        return self.state["pending_order"]
    
    def has_pending_order(self):
        """Cek apakah ada pending order."""
        return self.state["pending_order"] is not None
    
    def is_order_expired(self):
        """Cek apakah pending order sudah expired (timeout)."""
        order = self.state["pending_order"]
        if not order:
            return False
        timeout_min = order.get("timeout_minutes", config.ORDER_TIMEOUT_MINUTES)
        elapsed = time.time() - order["placed_time"]
        return elapsed > (timeout_min * 60)
    
    # =========================================================================
    # Trailing Stop Management
    # =========================================================================
    
    def set_trailing_stop(self, stop_order_id, stop_price, checkpoint_level, amount=None):
        """Simpan info trailing stop aktif."""
        self.state["trailing_stop"] = {
            "order_id": stop_order_id,
            "stop_price": stop_price,
            "checkpoint_level": checkpoint_level,
            "amount": amount,
            "set_time": datetime.now().isoformat(),
            "deduplicated": False,
        }
        self.state["current_checkpoint"] = checkpoint_level
        self.state["protection_status"] = "protected"
        self.save()
        logger.info(
            f"🛡️ Trailing stop saved: checkpoint {checkpoint_level}%, "
            f"stop @ {stop_price}"
        )
    
    def get_trailing_stop(self):
        """Ambil info trailing stop aktif."""
        return self.state["trailing_stop"]
    
    def get_current_checkpoint(self):
        """Ambil level checkpoint saat ini."""
        return self.state["current_checkpoint"]

    def mark_stop_deduplicated(self):
        stop = self.state.get("trailing_stop")
        if stop and not stop.get("deduplicated"):
            stop["deduplicated"] = True
            self.save()
    
    # =========================================================================
    # Signal & Misc
    # =========================================================================
    
    def set_last_signal(self, signal_data):
        """Simpan signal terakhir."""
        self.state["last_signal"] = {
            **signal_data,
            "time": datetime.now().isoformat(),
        }
        self.save()

    def set_scan_monitor(self, snapshot):
        """Publikasikan progres scan secara atomik untuk dashboard."""
        with self._lock:
            self.state["scan_monitor"] = copy.deepcopy(snapshot)
            self.state["last_scan_time"] = snapshot.get("updated_at")
            self.save()
    
    def set_status(self, status):
        """Update status bot."""
        self.state["bot_status"] = status
        self.save()

    def set_protection_status(self, status, error=None):
        """Catat invariant proteksi posisi secara eksplisit."""
        if self.state.get("protection_status") == status and not error:
            return
        self.state["protection_status"] = status
        if error:
            self.state["last_api_error"] = {
                "time": datetime.now().isoformat(),
                "message": str(error),
            }
        self.save()

    def set_connection_status(self, status, error=None):
        """Catat kesehatan REST/WebSocket tanpa mengubah state posisi."""
        if (
            self.state.get("connection_status") == status
            and not error
            and not self.state.get("last_api_error")
        ):
            return
        self.state["connection_status"] = status
        if error:
            self.state["last_api_error"] = {
                "time": datetime.now().isoformat(),
                "message": str(error),
            }
        elif status == "connected":
            self.state["last_api_error"] = None
        self.save()

    def mark_reconciled(self, result):
        self.state["last_reconciliation"] = {
            "time": datetime.now().isoformat(),
            "result": result,
        }
        self.save()

    def mark_websocket_event(self, event_type, status="connected"):
        self.state["last_websocket_event"] = {
            "time": datetime.now().isoformat(),
            "type": event_type,
        }
        self.state["connection_status"] = status
        self.save()

    def sync_position(
        self, symbol, side, entry_price, amount, order_id="synced_from_exchange",
        initial_stop_plan=None, entry_signal_snapshot=None,
    ):
        """Sinkronkan ukuran posisi tanpa mereset umur posisi yang sama."""
        current = self.state.get("active_position")
        if current and current.get("symbol") == symbol and current.get("side") == side:
            amount_changed = abs(float(current.get("amount", 0)) - float(amount)) > 1e-12
            current["entry_price"] = float(entry_price)
            current["amount"] = float(amount)
            if order_id:
                current["order_id"] = order_id
            if amount_changed:
                self.state["protection_status"] = "pending"
            if initial_stop_plan is not None:
                current["initial_stop_plan"] = copy.deepcopy(initial_stop_plan)
            if entry_signal_snapshot is not None:
                current["entry_signal_snapshot"] = copy.deepcopy(entry_signal_snapshot)
            self.state["bot_status"] = "monitoring"
            self.save()
            return
        self.set_position(
            symbol, side, entry_price, amount, order_id,
            initial_stop_plan=initial_stop_plan,
            entry_signal_snapshot=entry_signal_snapshot,
        )
    
    def get_state(self):
        """Ambil seluruh state (untuk dashboard)."""
        with self._lock:
            return copy.deepcopy(self.state)
    
    def update_highest_profit(self, profit_pct):
        """Update highest profit yang pernah dicapai posisi ini."""
        pos = self.state["active_position"]
        if not pos:
            return
        changed = False
        if profit_pct > pos.get("highest_profit_pct", 0):
            pos["highest_profit_pct"] = profit_pct
            changed = True
        if profit_pct < pos.get("lowest_profit_pct", 0):
            pos["lowest_profit_pct"] = profit_pct
            changed = True
        if changed:
            self.save()

    def record_partial_exit(self, amount, pnl, reason, flag=None):
        """Catat partial close tanpa mengakhiri lifecycle trade."""
        pos = self.state.get("active_position")
        if not pos:
            return
        pos["amount"] = max(0.0, float(pos.get("amount", 0)) - float(amount))
        pos["realized_partial_pnl"] = float(pos.get("realized_partial_pnl", 0)) + float(pnl)
        pos.setdefault("partial_exits", []).append({
            "time": datetime.now().isoformat(), "amount": float(amount),
            "pnl": float(pnl), "reason": reason,
        })
        if flag:
            pos[flag] = True
        self.state["protection_status"] = "pending"
        self.save()

    def begin_exit_intent(self, intent):
        """Durably record intent BEFORE network submission."""
        with self._lock:
            if self.state.get("exit_intent"):
                raise RuntimeError("Unresolved exit intent; reconcile first")
            self.state["exit_intent"] = copy.deepcopy(intent)
            self.save(strict=True)

    def get_exit_intent(self):
        with self._lock:
            return copy.deepcopy(self.state.get("exit_intent"))

    def finish_exit_intent(self, client_id, order, pnl):
        """Commit fill accounting + clear intent in one atomic write."""
        with self._lock:
            intent = self.state.get("exit_intent")
            if not intent or intent["client_id"] != client_id:
                return
            previous = copy.deepcopy(self.state)
            pos = self.state.get("active_position")
            if not pos or pos["symbol"] != intent["symbol"] or (
                intent.get("entry_order_id") is not None
                and str(pos.get("order_id")) != str(intent["entry_order_id"])
            ):
                raise ValueError("Exit intent belongs to a different or missing lifecycle")
            filled = float(order.get("filled") or 0)
            if filled > 0 and pos and pos["symbol"] == intent["symbol"]:
                pos["amount"] = min(float(pos["amount"]), max(0, intent["before_amount"] - filled))
                pos["realized_partial_pnl"] = float(pos.get("realized_partial_pnl", 0)) + pnl
                pos.setdefault("partial_exits", []).append({
                    "time": datetime.now().isoformat(), "amount": filled, "pnl": pnl,
                    "reason": intent["reason"], "order_id": order["id"],
                    "client_order_id": client_id, "pnl_status": "estimated",
                })
                if intent.get("flag"):
                    pos[intent["flag"]] = True
                self.state["protection_status"] = "pending"
            self.state["exit_intent"] = None
            try:
                self.save(strict=True)
            except OSError:
                self.state = previous
                raise

    def set_initial_risk(self, stop_plan):
        """Backfill initial risk untuk posisi hasil reconciliation/state lama."""
        pos = self.state.get("active_position")
        if not pos or not stop_plan:
            return
        stop_price = float(stop_plan.get("stop_price") or 0)
        entry = float(pos.get("entry_price") or 0)
        if stop_price <= 0 or entry <= 0:
            return
        pos["initial_stop_plan"] = copy.deepcopy(stop_plan)
        pos["initial_stop_price"] = stop_price
        pos["initial_risk_pct"] = abs(entry - stop_price) / entry * 100
        pos["initial_risk_usdt"] = (
            float(pos.get("initial_amount") or pos.get("amount") or 0)
            * abs(entry - stop_price)
        )
        self.save()

    def mark_pending_validated(self):
        pending = self.state.get("pending_order")
        if pending:
            pending["last_signal_validation"] = time.time()
            self.save()

    def update_trade_ledger(self, order_id, close_time, ledger=None, error=None):
        """Replace lifecycle estimate, not add it to partial PnL again."""
        with self._lock:
            for trade in self.state["trade_history"]:
                if str(trade.get("order_id")) != str(order_id) or trade.get("close_time") != close_time:
                    continue
                trade["ledger_last_attempt"] = time.time()
                if ledger is None:
                    trade["ledger_error"] = str(error)
                    trade.setdefault("pnl_status", "estimated")
                else:
                    old = float(trade["pnl"])
                    trade["ledger"] = copy.deepcopy(ledger)
                    trade["pnl"] = ledger["pnl"]
                    trade["pnl_status"] = ledger["status"]
                    trade.pop("ledger_error", None)
                    risk = float(trade.get("initial_risk_usdt") or 0)
                    trade["realized_r"] = ledger["pnl"] / risk if risk > 0 else None
                    self.state["total_profit"] += ledger["pnl"] - old
                    self.state["risk_circuit_last_trigger_trade"] = 0
                    losses = 0
                    for item in reversed(self.state["trade_history"]):
                        if float(item["pnl"]) < 0:
                            losses += 1
                        elif item.get("realized_r") is None or item["realized_r"] >= config.MEANINGFUL_WIN_R:
                            break
                    self.state["consecutive_losses"] = losses
                self.save(strict=True)
                return

    def evaluate_risk_circuit(self):
        """Pause entry baru setelah batas loss-R harian/rolling terlampaui."""
        if not getattr(config, "RISK_CIRCUIT_ENABLED", True):
            return False, ""
        trades = self.state.get("trade_history", [])
        trade_count = int(self.state.get("total_trades", len(trades)))
        if trade_count <= int(self.state.get("risk_circuit_last_trigger_trade", 0)):
            return False, ""

        today = datetime.now().date()
        daily_r = 0.0
        for trade in trades:
            try:
                if datetime.fromisoformat(trade.get("close_time", "")).date() == today:
                    daily_r += float(trade.get("realized_r") or 0)
            except (TypeError, ValueError):
                continue

        reason = ""
        if daily_r <= -float(getattr(config, "DAILY_MAX_LOSS_R", 2.0)):
            reason = f"daily_loss_{daily_r:.2f}R"
        window = int(getattr(config, "ROLLING_RISK_WINDOW", 10))
        recent = [float(t.get("realized_r")) for t in trades[-window:] if t.get("realized_r") is not None]
        if not reason and len(recent) >= int(getattr(config, "ROLLING_MIN_TRADES", 6)):
            gross_win = sum(r for r in recent if r > 0)
            gross_loss = abs(sum(r for r in recent if r < 0))
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
            if pf < float(getattr(config, "ROLLING_MIN_PROFIT_FACTOR", 0.8)):
                reason = f"rolling_profit_factor_{pf:.2f}"
        if not reason:
            return False, ""

        self.state["risk_circuit_last_trigger_trade"] = trade_count
        self.save()
        self.set_cooldown(
            minutes=float(getattr(config, "RISK_CIRCUIT_PAUSE_MINUTES", 360)),
            reason=f"risk_circuit:{reason}", ignore_loss_multiplier=True,
        )
        return True, reason
