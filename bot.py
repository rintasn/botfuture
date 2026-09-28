"""
Binance Futures Trading Bot - Main Orchestrator
================================================
Main loop yang mengatur semua module:
1. Scan market → 2. Analisis signal → 3. Place order → 
4. Monitor trailing stop → 5. Cek reversal

Flow:
- Jika TIDAK ada posisi/order → Scan & cari signal baru (jika tidak dalam cooldown)
- Jika ada PENDING ORDER → Monitor timeout, cek fill
- Jika ada POSISI AKTIF → Monitor trailing stop & reversal guard (TF 15m)
"""

import os
import time
import sys
from datetime import datetime
import signal as os_signal
import ccxt
from runtime_config import config, strategy_cycle
from logger_setup import logger
from state_manager import StateManager
from scanner import MarketScanner
from signal_engine import SignalEngine
from order_manager import OrderManager
from trailing_manager import TrailingManager
from reversal_guard import ReversalGuard
from user_stream import UserDataStream


PID_FILE = "bot.pid"


def check_and_create_pid_file():
    """Cegah menjalankan bot secara ganda dengan file lock."""
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            try:
                os.kill(old_pid, 0)
                logger.critical(
                    f"🚨 Bot sudah berjalan dengan PID {old_pid}! Hentikan dulu proses tersebut."
                )
                sys.exit(1)
            except OSError:
                pass
        except Exception:
            pass
            
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid_file():
    """Hapus pid file saat shutdown."""
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except Exception:
            pass


class TradingBot:
    """Main trading bot orchestrator."""
    
    def __init__(self):
        self.running = False
        self.exchange = None
        self.state = None
        self.scanner = None
        self.signal_engine = None
        self.order_mgr = None
        self.trailing_mgr = None
        self.reversal_guard = None
        self.user_stream = None
        self._last_cooldown_log = 0
    
    def initialize(self):
        """Initialize semua komponen bot."""
        check_and_create_pid_file()
        
        logger.info("=" * 60)
        logger.info("🤖 BINANCE FUTURES TRADING BOT (Institutional TPLR)")
        logger.info("=" * 60)
        
        if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
            logger.error("❌ API keys belum di-set! Edit file .env")
            sys.exit(1)
        
        logger.info(f"🔗 Connecting to Binance ({config.TRADING_MODE})...")
        
        exchange_config = {
            "apiKey": config.BINANCE_API_KEY,
            "secret": config.BINANCE_API_SECRET,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                # load_markets() tidak perlu memanggil endpoint wallet/currency
                # SAPI yang membutuhkan permission di luar kebutuhan bot futures.
                "fetchCurrencies": False,
            },
        }
        
        if config.TRADING_MODE == "testnet":
            logger.info("  📋 MODE: TESTNET (paper trading)")
        else:
            logger.info("  💰 MODE: LIVE TRADING")
        
        self.exchange = ccxt.binance(exchange_config)
        if config.TRADING_MODE == "testnet":
            self.exchange.set_sandbox_mode(True)
        self.exchange.load_markets()
        logger.info("  ✅ Markets loaded")
        
        self.state = StateManager()
        
        self.scanner = MarketScanner(self.exchange)
        self.signal_engine = SignalEngine(self.exchange)
        self.order_mgr = OrderManager(self.exchange, self.state)
        self.trailing_mgr = TrailingManager(self.order_mgr, self.state)
        self.reversal_guard = ReversalGuard(
            self.signal_engine, self.order_mgr,
            self.trailing_mgr, self.state
        )
        
        logger.info(f"  ⚙️  Timeframe: {config.TRADING_TIMEFRAME} (HTF: {config.HIGHER_TIMEFRAME})")
        logger.info(f"  ⚙️  Leverage: {config.LEVERAGE}x | Margin: {config.MARGIN_MODE}")
        logger.info(f"  ⚙️  Cooldown Global: {config.SIGNAL_COOLDOWN_MINUTES}m | Symbol: {config.SYMBOL_COOLDOWN_MINUTES}m")
        logger.info(f"  ⚙️  Trailing Ratchet: Checkpoint {config.TRAILING_CHECKPOINT_PERCENT}%, Stop {config.TRAILING_FIRST_STOP_PERCENT}%")
        logger.info(
            f"  ⚙️  Adaptive Hard Stop: ATR/EMA55/Swing | "
            f"Max {config.INITIAL_STOP_MAX_DISTANCE_PERCENT}% | "
            f"Risk {config.RISK_PER_TRADE_PERCENT}% wallet "
            f"({'ON' if config.EMERGENCY_SL_ENABLED else 'OFF'})"
        )
        
        balance = self.order_mgr.get_available_balance()
        logger.info(f"  💰 Available balance: {balance:.2f} USDT")
        
        reconciliation = self.order_mgr.reconcile_startup()
        if reconciliation["status"] != "ok":
            raise RuntimeError(
                f"Startup reconciliation gagal; bot tidak boleh trading: "
                f"{reconciliation.get('error')}"
            )
        logger.info(f"  ✅ Reconciliation OK: {reconciliation.get('action')}")

        if self.state.has_position() and not self._ensure_stop_protection():
            logger.critical("🚨 Posisi belum terproteksi; scanning entry baru dinonaktifkan.")

        if getattr(config, "USER_STREAM_ENABLED", True):
            self.user_stream = UserDataStream(
                config.BINANCE_API_KEY,
                config.BINANCE_API_SECRET,
                config.TRADING_MODE,
            )
            self.user_stream.start()
            logger.info("  ✅ User-data WebSocket started (orders + positions)")
        
        logger.info("=" * 60)
        
        os_signal.signal(os_signal.SIGINT, self._shutdown_handler)
        os_signal.signal(os_signal.SIGTERM, self._shutdown_handler)
    
    def _shutdown_handler(self, signum, frame):
        """Handle graceful shutdown."""
        logger.info("\n🛑 Shutdown signal received. Stopping bot...")
        self.running = False
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    def run(self):
        """Main trading loop."""
        try:
            self.initialize()
        except Exception:
            remove_pid_file()
            raise
        self.running = True
        
        logger.info("🚀 Bot started! Entering main loop...\n")
        
        iteration = 0
        
        while self.running:
            try:
                iteration += 1
                
                with strategy_cycle():
                    self._drain_user_stream()
                    had_pending = self.state.has_pending_order()
                    if had_pending:
                        self._handle_pending_order()
                    if self.state.has_position() and not had_pending:
                        self._handle_active_position()
                    elif not self.state.has_pending_order():
                        self.order_mgr.reconcile_trade_ledger()
                        self._scan_and_trade()
                
                time.sleep(config.MAIN_LOOP_INTERVAL)
                
            except KeyboardInterrupt:
                logger.info("🛑 KeyboardInterrupt. Stopping...")
                break
            except Exception as e:
                logger.error(f"❌ Error in main loop: {e}", exc_info=True)
                time.sleep(15)
        
        self._shutdown()

    def _drain_user_stream(self):
        """Consume WS hints; REST checks below remain authoritative."""
        if not self.user_stream:
            return
        for event in self.user_stream.drain():
            event_type = event.get("type")
            if event_type == "health":
                status = event.get("status")
                if status == "connected":
                    self.state.set_connection_status("connected")
                else:
                    self.state.set_connection_status(
                        "degraded", event.get("error", "WebSocket disconnected")
                    )
            elif event_type in ("orders", "positions"):
                self.state.mark_websocket_event(event_type)
    
    # =========================================================================
    # Handle Active Position
    # =========================================================================
    
    def _handle_active_position(self):
        """Monitor posisi aktif: trailing stop & reversal guard."""
        if not self.order_mgr.recover_exit_intent():
            return
        pos = self.state.get_position()
        if not pos:
            return
        
        symbol = pos["symbol"]
        side = pos["side"]
        entry_price = pos["entry_price"]
        amount = pos["amount"]

        position_result = self.order_mgr.get_position_status(symbol)
        if position_result["status"] == "error":
            logger.error(
                f"❌ API_ERROR saat verifikasi {symbol}; state posisi dipertahankan dan cycle dipause."
            )
            return
        if position_result["status"] == "empty":
            logger.warning(
                f"⚠️ Posisi {symbol} sudah tidak ada di Binance. Sinkronisasi state lokal."
            )
            current_price = self.order_mgr.get_current_price(symbol)
            real_pnl = self.order_mgr.get_realized_pnl(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                amount=amount,
                fallback_close_price=current_price,
            )
            self.order_mgr.cancel_all_orders(symbol)
            self.state.clear_position(
                pnl=real_pnl if amount > 0 else 0,
                reason="position_closed_on_exchange",
                start_cooldown=True,
            )
            return

        exchange_pos = position_result["position"]
        if abs(exchange_pos["contracts"] - amount) > 0.0001:
            logger.warning(
                f"⚠️ Amount mismatch! State: {amount}, Exchange: {exchange_pos['contracts']}. Syncing..."
            )
            self.state.sync_position(
                symbol,
                exchange_pos["side"],
                exchange_pos["entry_price"],
                exchange_pos["contracts"],
                order_id=pos.get("order_id"),
            )
            pos = self.state.get_position()
            amount = pos["amount"]
            entry_price = pos["entry_price"]

        if not self._ensure_stop_protection():
            return
        
        current_price = self.order_mgr.get_current_price(symbol)
        if current_price <= 0:
            logger.warning(f"⚠️ Gagal ambil harga {symbol}, skip cycle ini")
            return
        
        profit_pct = self.trailing_mgr.calculate_profit_pct(
            entry_price, current_price, side
        )
        checkpoint = self.state.get_current_checkpoint()
        
        # Hitung PnL dalam USDT dan ROE%
        if side == "long":
            unrealized_pnl_usdt = (current_price - entry_price) * amount
        else:
            unrealized_pnl_usdt = (entry_price - current_price) * amount
            
        roe_pct = profit_pct * config.LEVERAGE
        
        logger.info(
            f"📊 POSISI: {side.upper()} {symbol} | "
            f"Entry: {entry_price} | Now: {current_price} | "
            f"PnL: {unrealized_pnl_usdt:+.2f} USDT ({roe_pct:+.2f}% ROE {config.LEVERAGE}x | {profit_pct:+.2f}% Price) | "
            f"Checkpoint: {checkpoint}%"
        )
        
        # Trailing Stop Ratchet Update
        trail_result = self.trailing_mgr.update(
            symbol, entry_price, current_price, side, amount
        )
        
        if trail_result["action"] == "stop_triggered":
            logger.warning("⚠️ Stop triggered detected! Verifying position...")
            verify_result = self.order_mgr.get_position_status(symbol)
            if verify_result["status"] == "empty":
                real_pnl = self.order_mgr.get_realized_pnl(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    amount=amount,
                    fallback_close_price=current_price
                )
                self.order_mgr.cancel_all_orders(symbol)
                self.state.clear_position(
                    pnl=real_pnl, reason="trailing_stop_triggered", start_cooldown=True
                )
                logger.info(
                    f"💰 TRADE FINISHED (Trailing Stop) | {side.upper()} {symbol} | "
                    f"Realized PnL: {real_pnl:+.2f} USDT"
                )
            elif verify_result["status"] == "error":
                logger.error("❌ Status posisi sesudah stop tidak diketahui; state dipertahankan.")
            return

        if trail_result["action"] in ("protection_missing", "protection_unknown"):
            if trail_result["action"] == "protection_missing":
                self._ensure_stop_protection()
            return

        if trail_result["action"] in (
            "partial_tp1", "partial_tp2", "partial_tp1_pending", "partial_tp2_pending",
            "early_bep", "time_stop_closed"
        ):
            return
        
        # Reversal Guard (15m Timeframe)
        reversal_result = self.reversal_guard.check_and_act()
        if reversal_result["action"] in ("closed", "reduced"):
            logger.info(
                f"🔴 Position closed by reversal guard: {reversal_result['reason']}"
            )
            return

        # Stagnation Timeout Check (Anti-Sideways Protection)
        # CATATAN: Posisi dibiarkan berjalan (let winners run) sepenuhnya dikawal Trailing Stop (by Price & by Time).
        # Force Market Close hanya aktif jika STAGNATION_EXIT_ENABLED = True dan posisi belum pernah tembus checkpoint.
        if getattr(config, "STAGNATION_EXIT_ENABLED", False):
            entry_time_str = pos.get("entry_time")
            if entry_time_str:
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str)
                    elapsed_hours = (datetime.now() - entry_dt).total_seconds() / 3600.0
                except Exception:
                    elapsed_hours = 0.0
                    
                max_hours = getattr(config, "MAX_STAGNANT_HOURS", 3.0)
                min_stagnant_profit = getattr(config, "MAX_STAGNANT_MIN_PROFIT_PERCENT", 0.2)
                current_cp = self.state.get_current_checkpoint() or 0
                
                # Hanya close jika posisi macet dekat 0% dan belum mencapai checkpoint
                if elapsed_hours >= max_hours and current_cp == 0:
                    if profit_pct >= min_stagnant_profit:
                        logger.info(
                            f"⏰ STAGNATION TIMEOUT ({elapsed_hours:.1f}h >= {max_hours}h)! "
                            f"Posisi macet di profit +{profit_pct:.2f}%. Mengamankan profit via market close..."
                        )
                        close_res = self.order_mgr.close_position(
                            symbol=symbol,
                            side=side,
                            amount=amount,
                            reason=f"stagnation_timeout_profit_{profit_pct:.2f}%",
                        )
                        if close_res:
                            real_pnl = self.order_mgr.get_realized_pnl(
                                symbol=symbol,
                                side=side,
                                entry_price=entry_price,
                                amount=amount,
                                fallback_close_price=current_price,
                            )
                            self.state.clear_position(
                                pnl=real_pnl,
                                reason=f"stagnation_timeout (+{profit_pct:.2f}%)",
                                start_cooldown=True,
                            )
                            logger.info(
                                f"💰 TRADE FINISHED (Stagnation Timeout) | {side.upper()} {symbol} | "
                                f"Realized PnL: {real_pnl:+.2f} USDT"
                            )
                            return
    
    # =========================================================================
    # Handle Pending Order
    # =========================================================================
    
    def _handle_pending_order(self):
        """Monitor pending limit order: cek fill atau timeout."""
        order = self.state.get_pending_order()
        if not order:
            return
        
        symbol = order["symbol"]
        order_id = order["order_id"]
        timeout_min = order.get("timeout_minutes", config.ORDER_TIMEOUT_MINUTES)

        # Heartbeat exchange-side kill switch. Jika jaringan mati, countdown dan
        # GTD akan membatalkan remainder tanpa menunggu proses Python pulih.
        self.order_mgr.refresh_entry_deadman(symbol)

        if self.state.is_order_expired():
            logger.info(
                f"⏰ Limit Order timeout ({timeout_min} min)! "
                f"Canceling order {order_id} for {symbol}"
            )
            cancel_result = self.order_mgr.cancel_order(symbol, order_id)
            if cancel_result in ("filled", "partial") and self.state.has_position():
                self._ensure_stop_protection()
            return
        
        status = self.order_mgr.check_order_filled(symbol, order_id)
        
        if status in ("filled", "partial", "partial_open"):
            logger.info(
                f"🎯 Order {'FULLY' if status == 'filled' else 'PARTIALLY'} FILLED! "
                f"{symbol} @ {order.get('price')}. Starting position monitoring..."
            )
            pos = self.state.get_position()
            if pos:
                self._ensure_stop_protection()
                
        elif status == "open":
            revalidate_every = float(getattr(config, "PENDING_REVALIDATION_SECONDS", 30))
            last_validation = float(order.get("last_signal_validation", 0) or 0)
            if time.time() - last_validation >= revalidate_every:
                validation = self.signal_engine.validate_pending_entry(symbol, order["side"])
                self.state.mark_pending_validated()
                if not validation["valid"]:
                    logger.warning(
                        f"Pending entry {symbol} dibatalkan: {validation['reason']}"
                    )
                    self.order_mgr.cancel_order(symbol, order_id)
                    return
            elapsed = time.time() - order["placed_time"]
            remaining = (timeout_min * 60) - elapsed
            logger.info(
                f"⏳ Waiting limit fill: {symbol} @ {order['price']} | "
                f"Remaining: {remaining/60:.1f} min"
            )
    
    def _ensure_stop_protection(self):
        """Invariant: posisi exchange harus mempunyai stop aktif sebelum dikelola."""
        pos = self.state.get_position()
        if not pos:
            return True
        if not getattr(config, "MANDATORY_STOP_PROTECTION", True):
            return True

        stop = self.state.get_trailing_stop()
        if stop and stop.get("order_id"):
            in_grace = getattr(
                self.trailing_mgr, "is_within_verification_grace", lambda _: False
            )(stop)
            if in_grace:
                stop_status = "active"
            else:
                stop_status = self.trailing_mgr._verify_stop_order(
                    pos["symbol"], stop["order_id"]
                )
            if stop_status == "active":
                if (
                    getattr(config, "STOP_DEDUPLICATION_ENABLED", True)
                    and not stop.get("deduplicated")
                    and not in_grace
                ):
                    try:
                        adopted = self.order_mgr.deduplicate_stop_algos(
                            pos["symbol"], pos["side"], preferred_id=stop["order_id"]
                        )
                        if adopted and str(adopted["id"]) != str(stop["order_id"]):
                            self.state.set_trailing_stop(
                                adopted["id"], adopted["stop_price"],
                                stop.get("checkpoint_level", 0),
                                amount=adopted.get("amount") or pos["amount"],
                            )
                            stop = self.state.get_trailing_stop()
                        self.state.mark_stop_deduplicated()
                    except Exception as e:
                        logger.warning(f"⚠️ Deduplikasi stop ditunda: {e}")
                protected_amount = stop.get("amount")

                # Migrasikan legacy fixed stop yang terlalu jauh. Stop adaptif
                # dibuat dahulu; legacy stop baru dibatalkan setelah create ACK.
                stop_price = float(stop.get("stop_price") or 0)
                stop_distance = (
                    abs(float(pos["entry_price"]) - stop_price)
                    / float(pos["entry_price"]) * 100
                    if stop_price > 0 and float(pos["entry_price"]) > 0
                    else 0
                )
                max_distance = float(
                    getattr(config, "INITIAL_STOP_MAX_DISTANCE_PERCENT", 5.0)
                )
                if (
                    float(stop.get("checkpoint_level") or 0) == 0
                    and stop_distance > max_distance + 1e-9
                ):
                    logger.warning(
                        f"⚠️ Legacy stop {stop_distance:.2f}% terlalu jauh; "
                        f"migrasi ke adaptive stop (max {max_distance:.2f}%)."
                    )
                    old_stop_id = stop["order_id"]
                    if self._place_emergency_sl(pos, fail_close=False):
                        if str((self.state.get_trailing_stop() or {}).get("order_id")) != str(old_stop_id):
                            self.order_mgr.cancel_stop_order(pos["symbol"], old_stop_id)
                        return True
                    # Stop legacy tetap aktif; jangan fail-close atau menandai
                    # posisi unprotected hanya karena tightening gagal.
                    self.state.set_protection_status("protected")
                    return True

                if (
                    protected_amount is not None
                    and abs(float(protected_amount) - float(pos["amount"])) <= 1e-12
                ):
                    self.state.set_protection_status("protected")
                    return True
                logger.warning("⚠️ Ukuran stop tidak sama dengan posisi; mengganti stop secara aman.")
                old_stop_id = stop["order_id"]
                if self._place_emergency_sl(pos, fail_close=False):
                    if str((self.state.get_trailing_stop() or {}).get("order_id")) != str(old_stop_id):
                        self.order_mgr.cancel_stop_order(pos["symbol"], old_stop_id)
                    return True
                self.state.set_protection_status("failed", "Stop tidak mencakup seluruh posisi")
                if getattr(config, "FAIL_CLOSE_IF_STOP_UNPROTECTED", True):
                    self.order_mgr.close_position(
                        pos["symbol"], pos["side"], pos["amount"],
                        reason="stop_size_mismatch",
                    )
                return False
            if stop_status == "triggered":
                self.state.set_protection_status("unknown")
                return False
            if stop_status == "unknown":
                # Jangan membuat stop duplikat atau menganggap posisi kosong saat API putus.
                self.state.set_protection_status("unknown", "Stop status tidak dapat diverifikasi")
                return False

            # Jika tracked id belum terlihat/ternyata canceled, adopsi stop aktif
            # lain dan hapus duplikat sebelum membuat order baru.
            try:
                adopted = self.order_mgr.deduplicate_stop_algos(
                    pos["symbol"], pos["side"], preferred_id=stop["order_id"]
                )
                if adopted:
                    self.state.set_trailing_stop(
                        adopted["id"], adopted["stop_price"],
                        stop.get("checkpoint_level", 0),
                        amount=adopted.get("amount") or pos["amount"],
                    )
                    return True
            except Exception as e:
                self.state.set_protection_status("unknown", e)
                return False
            self.state.set_protection_status("missing")

        return self._place_emergency_sl(pos)

    def _place_emergency_sl(self, pos, fail_close=True):
        """Pasang adaptive exchange hard-stop; fallback dibatasi max distance."""
        symbol = pos["symbol"]
        entry = pos["entry_price"]
        side = pos["side"]
        amount = pos["amount"]
        
        plan = pos.get("initial_stop_plan")
        max_pct = float(getattr(config, "INITIAL_STOP_MAX_DISTANCE_PERCENT", 5.0))
        plan_valid = bool(plan and float(plan.get("stop_price") or 0) > 0)
        if plan_valid:
            plan_distance = abs(entry - float(plan["stop_price"])) / entry * 100
            plan_valid = (
                plan_distance <= max_pct + 1e-9
                and ((side == "long" and float(plan["stop_price"]) < entry)
                     or (side == "short" and float(plan["stop_price"]) > entry))
            )
        if not plan_valid:
            plan = (
                self.signal_engine.calculate_initial_stop(symbol, side, entry)
                if self.signal_engine is not None
                else None
            )
            plan_valid = bool(plan and float(plan.get("stop_price") or 0) > 0)

        if plan_valid:
            sl_price = float(plan["stop_price"])
            distance_pct = abs(entry - sl_price) / entry * 100
            source = plan.get("method", "atr_ema55_swing")
        else:
            distance_pct = max_pct
            sl_price = (
                entry * (1 - max_pct / 100)
                if side == "long"
                else entry * (1 + max_pct / 100)
            )
            source = "max_distance_fallback"

        effective_plan = dict(plan or {})
        effective_plan.update({
            "stop_price": sl_price,
            "distance_pct": distance_pct,
            "method": source,
        })
        if not pos.get("initial_risk_pct"):
            self.state.set_initial_risk(effective_plan)
        # Resizing/recovering protection must never surrender a locked profit.
        previous_stop = self.state.get_trailing_stop() or {}
        previous_price = float(previous_stop.get("stop_price") or 0)
        if previous_price > 0:
            sl_price = max(sl_price, previous_price) if side == "long" else min(sl_price, previous_price)
            
        logger.info(
            f"🛡️ Placing adaptive hard-stop for {symbol} at {sl_price:.8f} "
            f"({distance_pct:.2f}% from entry | {source})"
        )
        
        self.state.set_protection_status("pending")
        stop_order = self.order_mgr.place_stop_order(
            symbol=symbol,
            side=side,
            amount=amount,
            stop_price=sl_price,
        )
        
        if stop_order:
            self.state.set_trailing_stop(
                stop_order_id=stop_order["id"],
                stop_price=sl_price,
                checkpoint_level=float(previous_stop.get("checkpoint_level") or self.state.get_current_checkpoint() or 0),
                amount=amount,
            )
            logger.info(f"✅ Emergency SL active on Binance! ID: {stop_order['id']}")
            return True
        else:
            logger.error(f"❌ Gagal pasang emergency SL untuk {symbol}")
            self.state.set_protection_status("failed", "Gagal memasang emergency stop")
            if fail_close and getattr(config, "FAIL_CLOSE_IF_STOP_UNPROTECTED", True):
                logger.critical(
                    f"🚨 FAIL-CLOSE: mencoba menutup {symbol} karena posisi tanpa stop."
                )
                self.order_mgr.close_position(
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    reason="stop_protection_failed",
                )
            return False

    # =========================================================================
    # Scan & Trade
    # =========================================================================
    
    def _scan_and_trade(self):
        """Scan market, analisis signal, dan place order jika ada signal."""
        if self.state.has_position() or self.state.has_pending_order() or self.state.get_exit_intent():
            return
        now = time.time()
        
        # 1. Cek Cooldown
        is_cooldown, rem_sec, reason = self.state.is_cooldown_active()
        if is_cooldown:
            if now - self._last_cooldown_log >= 60:
                if rem_sec >= 999900:
                    logger.warning(f"🛑 Emergency Pause aktif: {reason}")
                else:
                    logger.info(f"⏳ Cooldown aktif ({int(rem_sec)}s tersisa). Alasan: {reason}")
                self._last_cooldown_log = now
            self.state.set_status("cooldown")
            return

        circuit_active, circuit_reason = self.state.evaluate_risk_circuit()
        if circuit_active:
            logger.critical(f"Risk circuit breaker aktif: {circuit_reason}")
            self.state.set_status("cooldown")
            return
            
        # 2. Cek Saldo Minimum
        balance = self.order_mgr.get_available_balance()
        if balance < config.MIN_WALLET_BALANCE_USDT:
            if now - self._last_cooldown_log >= 60:
                logger.warning(
                    f"⚠️ Saldo {balance:.2f} USDT dibawah batas minimum {config.MIN_WALLET_BALANCE_USDT} USDT. "
                    f"Menunggu isi saldo."
                )
                self._last_cooldown_log = now
            self.state.set_status("insufficient_balance")
            return
            
        self.state.set_status("scanning")
        
        candidates = self.scanner.scan()
        scan_started = time.time()
        monitor = {
            "status": "scanning" if candidates else (
                "error" if getattr(self.scanner, "last_error", None) else "complete"
            ),
            "started_at": scan_started,
            "updated_at": scan_started,
            "completed_at": None if candidates else scan_started,
            "universe_count": getattr(self.scanner, "last_universe_count", 0),
            "ranked_count": getattr(self.scanner, "last_top_count", 0),
            "total_candidates": len(candidates),
            "processed_count": 0,
            "accepted_count": 0,
            "error": getattr(self.scanner, "last_error", None),
            "markets": [
                {
                    "symbol": item["symbol"],
                    "price": item.get("price"),
                    "quote_volume": item.get("quote_volume"),
                    "spread_pct": item.get("spread_pct"),
                    "change_24h": item.get("change_24h"),
                    "scan_score": item.get("scan_score"),
                    "status": "queued", "signal": "WAIT", "score": 0,
                    "reason": "", "higher_tf_bias": "neutral",
                    "btc_market_bias": "neutral", "btcdom_bias": "neutral",
                }
                for item in candidates
            ],
        }
        self.state.set_scan_monitor(monitor)
        if not candidates:
            self.state.set_status("idle")
            return
        
        best_signal = None
        last_publish = scan_started
        
        for index, candidate in enumerate(candidates):
            symbol = candidate["symbol"]
            row = monitor["markets"][index]
            
            is_sym_cd, _, _ = self.state.is_cooldown_active(symbol)
            if is_sym_cd:
                row.update(status="cooldown", reason="symbol_cooldown")
            else:
                signal_result = self.signal_engine.analyze(symbol)
                row.update(
                    price=signal_result.get("price") or row["price"],
                    signal=signal_result.get("signal", "WAIT"),
                    score=signal_result.get("score", 0),
                    reason=signal_result.get("details", {}).get("reason", ""),
                    higher_tf_bias=signal_result.get("higher_tf_bias", "neutral"),
                    btc_market_bias=signal_result.get("btc_market_bias", "neutral"),
                    btcdom_bias=signal_result.get("btcdom_bias", "neutral"),
                )
                if signal_result["signal"] in ("LONG", "SHORT") and signal_result["score"] >= config.SIGNAL_MIN_SCORE:
                    row["status"] = "qualified"
                    monitor["accepted_count"] += 1
                    signal_result["scan_score"] = candidate["scan_score"]
                    signal_result["selection_score"] = (
                        float(signal_result["score"]) * 0.9
                        + float(candidate["scan_score"]) * 0.1
                    )
                    if best_signal is None or signal_result["selection_score"] > best_signal["selection_score"]:
                        best_signal = signal_result
                elif signal_result["signal"] in ("LONG", "SHORT"):
                    row.update(status="below_score", reason="below_minimum_score")
                else:
                    row["status"] = "rejected"
            monitor["processed_count"] = index + 1
            if (index + 1) % 5 == 0 or time.time() - last_publish >= 2:
                monitor["updated_at"] = time.time()
                self.state.set_scan_monitor(monitor)
                last_publish = monitor["updated_at"]

        monitor["status"] = "complete"
        monitor["updated_at"] = monitor["completed_at"] = time.time()
        
        if best_signal:
            # A broad scan can outlive a candle. Recheck the winner before entry.
            fresh = self.signal_engine.analyze(best_signal["symbol"])
            if fresh["signal"] != best_signal["signal"] or fresh["score"] < config.SIGNAL_MIN_SCORE:
                logger.info("Selected entry invalidated during scan; skipping this cycle.")
                for row in monitor["markets"]:
                    if row["symbol"] == best_signal["symbol"]:
                        row.update(status="invalidated", reason="selection_revalidation_failed")
                        monitor["accepted_count"] = max(0, monitor["accepted_count"] - 1)
                        break
                self.state.set_scan_monitor(monitor)
                return
            best_signal = fresh
            for row in monitor["markets"]:
                if row["symbol"] == best_signal["symbol"]:
                    row.update(status="selected", score=best_signal["score"],
                               signal=best_signal["signal"])
                    break

        self.state.set_scan_monitor(monitor)

        if best_signal and best_signal["score"] >= config.SIGNAL_MIN_SCORE:
            symbol = best_signal["symbol"]
            signal = best_signal["signal"]
            price = best_signal["price"]
            score = best_signal["score"]
            
            logger.info(
                f"\n{'='*50}\n"
                f"🚀 ENTRY SIGNAL: {signal} {symbol}\n"
                f"   Score: {score}/100 | Price: {price}\n"
                f"   HTF Bias 1H: {best_signal['higher_tf_bias']}\n"
                f"   BTC.D Bias: {best_signal.get('btcdom_bias', 'neutral')} | "
                f"Projection: {best_signal.get('market_projection', 'balanced')}\n"
                f"   Details: {best_signal['details']}\n"
                f"{'='*50}"
            )
            
            self.state.set_last_signal(best_signal)
            self.state.set_status("trading")
            suggested_p = best_signal.get("suggested_entry_price")
            order = self.order_mgr.place_entry_order(
                symbol=symbol,
                signal=signal,
                current_price=price,
                score=score,
                suggested_price=suggested_p,
                initial_stop_plan=best_signal.get("initial_stop_plan"),
                signal_context=best_signal.get("entry_signal_snapshot"),
            )
            
            if order:
                logger.info(f"✅ Entry order placed! Waiting for fill...")
            else:
                self.state.set_status("idle")
        else:
            self.state.set_status("idle")
    
    # =========================================================================
    # Shutdown
    # =========================================================================
    
    def _shutdown(self):
        """Graceful shutdown."""
        if self.user_stream:
            self.user_stream.stop()
        remove_pid_file()
        logger.info("\n" + "=" * 60)
        logger.info("🛑 Bot shutting down...")
        
        state = self.state.get_state()
        logger.info(f"  📊 Total trades: {state.get('total_trades', 0)}")
        logger.info(f"  💰 Total profit: {state.get('total_profit', 0):.2f} USDT")
        
        self.state.save()
        logger.info("  💾 State saved")
        logger.info("=" * 60)
        logger.info("👋 Goodbye!\n")
