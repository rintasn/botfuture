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
import config


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
        }
    
    def _load_state(self):
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
                
                logger.info(f"📂 State loaded dari {self.state_file}")
                return state
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"⚠️ Gagal load state: {e}. Menggunakan default.")
        
        return self._default_state()
    
    def save(self):
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
                f"Cooldown panjang diaktifkan: {actual_minutes} menit (2 jam) sebelum auto-resume."
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
        
        # 1. Cek Global Cooldown (termasuk pause 2 jam losestreak)
        global_until = self.state.get("cooldown_until", 0)
        if now < global_until:
            rem = global_until - now
            losses = self.state.get("consecutive_losses", 0)
            if losses >= config.MAX_CONSECUTIVE_LOSSES:
                return True, rem, f"Losestreak pause ({int(rem/60)}m tersisa dari 2 jam)"
            return True, rem, f"Global cooldown ({int(rem)}s tersisa)"
        else:
            # Jika masa cooldown 2 jam sudah lewat, auto-reset consecutive losses
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
        initial_stop_plan=None,
    ):
        """Simpan info posisi aktif."""
        self.state["active_position"] = {
            "symbol": symbol,
            "side": side,                    # 'long' atau 'short'
            "entry_price": entry_price,
            "amount": amount,
            "order_id": order_id,
            "entry_time": entry_time or datetime.now().isoformat(),
            "highest_profit_pct": 0.0,
            "initial_stop_plan": copy.deepcopy(initial_stop_plan),
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
            trade_record = {
                **pos,
                "close_time": datetime.now().isoformat(),
                "pnl": pnl,
                "close_reason": reason,
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
        initial_stop_plan=None,
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
        initial_stop_plan=None,
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
            self.state["bot_status"] = "monitoring"
            self.save()
            return
        self.set_position(
            symbol, side, entry_price, amount, order_id,
            initial_stop_plan=initial_stop_plan,
        )
    
    def get_state(self):
        """Ambil seluruh state (untuk dashboard)."""
        with self._lock:
            return copy.deepcopy(self.state)
    
    def update_highest_profit(self, profit_pct):
        """Update highest profit yang pernah dicapai posisi ini."""
        pos = self.state["active_position"]
        if pos and profit_pct > pos.get("highest_profit_pct", 0):
            pos["highest_profit_pct"] = profit_pct
            self.save()
