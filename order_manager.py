"""
Order Manager
=============
Mengelola lifecycle order: place limit, cek fill, cancel, close posisi.
- Entry via LIMIT order (-0.1% / -0.35% Tiered)
- Stepped Trailing stop via STOP_MARKET
- Close via MARKET order
- Real Binance realized PnL extraction

PENTING: Semua order operations di-wrap dengan retry dan validasi
agar tidak terjadi state inconsistency.
"""

import time
import threading
import uuid
from datetime import datetime
import ccxt
from runtime_config import config
from logger_setup import logger
from trade_ledger import TradeLedger


class OrderManager:
    """Mengelola semua operasi order di Binance Futures."""
    
    MAX_RETRIES = 3
    RETRY_DELAY = 2  # detik
    
    def __init__(self, exchange, state_manager):
        self.exchange = exchange
        self.state = state_manager
        self._stop_lock = threading.RLock()
        self._last_ledger_check = 0

    def reconcile_trade_ledger(self):
        """Read-only, rate-bounded accounting catch-up while flat."""
        now = time.time()
        if now - self._last_ledger_check < 60:
            return
        self._last_ledger_check = now
        records = self.state.get_state().get("trade_history", [])
        eligible = [t for t in records if t.get("pnl_status") in ("estimated", "pending")
                    and now - float(t.get("ledger_last_attempt", 0)) >= 300
                    and now - datetime.fromisoformat(t["close_time"]).timestamp() >= 120]
        if not eligible:
            return
        trade = min(eligible, key=lambda t: float(t.get("ledger_last_attempt", 0)))
        try:
            ledger = TradeLedger(self.exchange).reconcile(trade)
        except Exception as exc:
            self.state.update_trade_ledger(trade.get("order_id"), trade["close_time"], error=exc)
            logger.warning("Ledger remains estimated for %s: %s", trade["symbol"], exc)
        else:
            self.state.update_trade_ledger(trade.get("order_id"), trade["close_time"], ledger=ledger)

    @staticmethod
    def _client_order_id(symbol):
        base = symbol.split("/")[0].lower()[:8]
        return f"{config.BOT_ORDER_CLIENT_PREFIX}{base}_{int(time.time() * 1000)}"[:36]

    def _raw_symbol(self, symbol):
        market = self.exchange.market(symbol)
        return market.get("id", symbol.replace("/", "").split(":")[0])

    def _fetch_order(self, symbol, order_id=None, client_order_id=None):
        """Fetch order by exchange id, atau origClientOrderId untuk outcome yang ambigu."""
        if order_id:
            return self.exchange.fetch_order(order_id, symbol)
        if not client_order_id:
            raise ccxt.OrderNotFound("order id dan client order id tidak tersedia")
        raw = self.exchange.fapiPrivateGetOrder({
            "symbol": self._raw_symbol(symbol),
            "origClientOrderId": client_order_id,
        })
        status_map = {
            "NEW": "open",
            "PARTIALLY_FILLED": "open",
            "FILLED": "closed",
            "CANCELED": "canceled",
            "EXPIRED": "expired",
            "REJECTED": "rejected",
        }
        return {
            "id": str(raw.get("orderId")) if raw.get("orderId") is not None else None,
            "clientOrderId": raw.get("clientOrderId"),
            "status": status_map.get(str(raw.get("status", "")).upper(), "unknown"),
            "filled": float(raw.get("executedQty") or 0),
            "amount": float(raw.get("origQty") or 0),
            "average": float(raw.get("avgPrice") or 0),
            "price": float(raw.get("price") or 0),
            "info": raw,
        }

    def _fetch_all_open_orders(self):
        """Ambil seluruh USD-M open orders tanpa memicu guard CCXT no-symbol."""
        response = self.exchange.fapiPrivateGetOpenOrders()
        if isinstance(response, dict):
            raw_orders = response.get("orders") or response.get("rows") or []
        else:
            raw_orders = response or []

        status_map = {
            "NEW": "open",
            "PARTIALLY_FILLED": "open",
            "FILLED": "closed",
            "CANCELED": "canceled",
            "EXPIRED": "expired",
            "REJECTED": "rejected",
        }
        orders = []
        for raw in raw_orders:
            raw_symbol = raw.get("symbol")
            try:
                symbol = self.exchange.safe_symbol(raw_symbol, None, None, "swap")
            except Exception:
                symbol = raw_symbol
            orders.append({
                "id": str(raw.get("orderId")) if raw.get("orderId") is not None else None,
                "clientOrderId": raw.get("clientOrderId"),
                "symbol": symbol,
                "side": str(raw.get("side") or "").lower(),
                "price": float(raw.get("price") or 0),
                "amount": float(raw.get("origQty") or 0),
                "filled": float(raw.get("executedQty") or 0),
                "status": status_map.get(str(raw.get("status") or "").upper(), "unknown"),
                "info": raw,
            })
        return orders

    @staticmethod
    def _algo_rows(response):
        if isinstance(response, dict):
            return response.get("orders") or response.get("rows") or []
        return response or []

    def get_active_stop_algos(self, symbol, position_side=None):
        """Ambil stop Algo Order aktif untuk simbol/sisi posisi tertentu."""
        raw_symbol = self._raw_symbol(symbol)
        try:
            try:
                response = self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": raw_symbol})
            except TypeError:
                response = self.exchange.fapiPrivateGetOpenAlgoOrders()
        except Exception as e:
            logger.warning(f"⚠️ Gagal mengambil open Algo Orders {symbol}: {e}")
            raise

        expected_side = None
        if position_side:
            expected_side = "SELL" if position_side.lower() == "long" else "BUY"
        result = []
        for algo in self._algo_rows(response):
            status = str(algo.get("algoStatus") or algo.get("status") or "").upper()
            order_type = str(algo.get("orderType") or algo.get("type") or "").upper()
            algo_side = str(algo.get("side") or "").upper()
            if algo.get("symbol") != raw_symbol:
                continue
            if status not in ("NEW", "ACTIVE") or "STOP" not in order_type:
                continue
            if expected_side and algo_side and algo_side != expected_side:
                continue
            result.append(algo)
        return result

    @staticmethod
    def _algo_id(algo):
        return algo.get("algoId") or algo.get("orderId") or algo.get("id")

    @staticmethod
    def _algo_trigger_price(algo):
        return float(algo.get("triggerPrice") or algo.get("stopPrice") or 0)

    @staticmethod
    def _algo_amount(algo):
        return float(algo.get("quantity") or algo.get("origQty") or algo.get("amount") or 0)

    def deduplicate_stop_algos(self, symbol, position_side, preferred_id=None):
        """Pastikan hanya satu STOP Algo Order aktif untuk posisi pada symbol."""
        active = self.get_active_stop_algos(symbol, position_side)
        if not active:
            return None

        preferred = str(preferred_id) if preferred_id is not None else None
        nonzero = [a for a in active if self._algo_trigger_price(a) > 0]
        candidates = nonzero or active
        reverse = position_side.lower() == "long"
        candidates = sorted(candidates, key=self._algo_trigger_price, reverse=reverse)
        best_price = self._algo_trigger_price(candidates[0])
        best = [a for a in candidates if self._algo_trigger_price(a) == best_price]
        keep = next(
            (a for a in best if str(self._algo_id(a)) == preferred), best[0]
        )

        keep_id = self._algo_id(keep)
        raw_symbol = self._raw_symbol(symbol)
        for duplicate in active:
            duplicate_id = self._algo_id(duplicate)
            if not duplicate_id or str(duplicate_id) == str(keep_id):
                continue
            try:
                self.exchange.fapiPrivateDeleteAlgoOrder({
                    "symbol": raw_symbol,
                    "algoId": int(duplicate_id),
                })
                logger.warning(
                    f"🧹 Duplicate STOP Algo Order {duplicate_id} dibatalkan; "
                    f"stop {keep_id} dipertahankan untuk {symbol}."
                )
            except Exception as e:
                logger.error(f"❌ Gagal membatalkan duplicate stop {duplicate_id}: {e}")

        return {
            "id": keep_id,
            "stop_price": self._algo_trigger_price(keep),
            "amount": self._algo_amount(keep),
            "info": keep,
        }

    def find_matching_stop_algo(self, symbol, position_side, amount, stop_price):
        """Temukan stop identik untuk recovery create timeout/eventual consistency."""
        rounded = float(self.exchange.price_to_precision(symbol, stop_price))
        amount = float(amount)
        for algo in self.get_active_stop_algos(symbol, position_side):
            price_match = abs(self._algo_trigger_price(algo) - rounded) <= max(1e-12, rounded * 1e-10)
            algo_amount = self._algo_amount(algo)
            amount_match = algo_amount <= 0 or abs(algo_amount - amount) <= max(1e-12, amount * 1e-10)
            if price_match and amount_match:
                return {
                    "id": self._algo_id(algo),
                    "stop_price": rounded,
                    "amount": algo_amount or amount,
                    "info": algo,
                    "adopted": True,
                }
        return None

    def refresh_entry_deadman(self, symbol, force=False):
        """Refresh Binance countdownCancelAll hanya selama ada pending entry bot."""
        if not getattr(config, "ENTRY_DEADMAN_ENABLED", False):
            return True
        pending = self.state.get_pending_order()
        if not pending or pending.get("symbol") != symbol:
            return True
        now = time.time()
        last = float(pending.get("last_deadman_refresh") or 0)
        if not force and now - last < config.ENTRY_DEADMAN_REFRESH_SECONDS:
            return True
        try:
            self.exchange.fapiPrivatePostCountdownCancelAll({
                "symbol": self._raw_symbol(symbol),
                "countdownTime": config.ENTRY_DEADMAN_COUNTDOWN_MS,
            })
            pending["last_deadman_refresh"] = now
            self.state.save()
            return True
        except Exception as e:
            logger.error(f"❌ Gagal refresh dead-man switch {symbol}: {e}")
            self.state.set_connection_status("api_error", e)
            return False

    def disable_entry_deadman(self, symbol):
        if not getattr(config, "ENTRY_DEADMAN_ENABLED", False):
            return True
        try:
            self.exchange.fapiPrivatePostCountdownCancelAll({
                "symbol": self._raw_symbol(symbol),
                "countdownTime": 0,
            })
            return True
        except Exception as e:
            logger.warning(f"⚠️ Gagal menonaktifkan dead-man switch {symbol}: {e}")
            return False
    
    # =========================================================================
    # Setup
    # =========================================================================
    
    def setup_symbol(self, symbol):
        """
        Set leverage dan margin mode untuk symbol tertentu.
        Harus dipanggil sebelum place order.
        """
        try:
            # Set margin mode (isolated)
            try:
                self.exchange.set_margin_mode(config.MARGIN_MODE, symbol)
                logger.info(f"  ✅ Margin mode: {config.MARGIN_MODE} for {symbol}")
            except ccxt.ExchangeError as e:
                # Biasanya error kalau sudah di-set sebelumnya
                if "No need to change" in str(e) or "already" in str(e).lower():
                    pass
                else:
                    logger.warning(f"  ⚠️ Set margin mode: {e}")
            
            # Set leverage
            self.exchange.set_leverage(config.LEVERAGE, symbol)
            logger.info(f"  ✅ Leverage: {config.LEVERAGE}x for {symbol}")
            
        except Exception as e:
            logger.error(f"❌ Gagal setup symbol {symbol}: {e}")
            raise
    
    # =========================================================================
    # Balance
    # =========================================================================
    
    def get_available_balance(self):
        """Ambil USDT balance yang tersedia untuk trading."""
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            free = usdt.get("free", 0)
            logger.info(f"  💰 Available USDT: {free:.2f}")
            return float(free)
        except Exception as e:
            logger.error(f"❌ Gagal fetch balance: {e}")
            return 0
    
    # =========================================================================
    # Entry Order (Limit)
    # =========================================================================
    
    def place_entry_order(
        self, symbol, signal, current_price, score=None, suggested_price=None,
        initial_stop_plan=None, signal_context=None,
    ):
        """
        Place limit order untuk entry posisi dengan Dynamic Confluence Limit & Tiered Fallback.
        - Jika suggested_price ada: gunakan harga optimal EMA21 / ATR pullback.
        - High Conviction (score >= 80): offset sangat rapat (0.1%), timeout 10m
        - Normal Conviction (score 70-79): offset tawar (0.35%), timeout 15m
        
        Args:
            symbol: Trading pair (e.g., 'BTC/USDT:USDT')
            signal: 'LONG' atau 'SHORT'
            current_price: Harga saat ini
            score: Skor sinyal (0-100)
            suggested_price: Harga entry optimal dari SignalEngine (EMA21/ATR pullback)
        
        Returns:
            dict | None: Order info jika berhasil, None jika gagal
        """
        try:
            # Cek apakah sudah ada posisi/order aktif
            if self.state.has_position():
                logger.warning("⚠️ Sudah ada posisi aktif, skip entry.")
                return None
            
            if self.state.has_pending_order():
                logger.warning("⚠️ Sudah ada pending order, skip entry.")
                return None
            
            # Double-check: cek posisi langsung dari Binance
            position_result = self.get_position_status(symbol)
            if position_result["status"] == "error":
                logger.error("❌ Entry dibatalkan: status posisi Binance tidak dapat diverifikasi.")
                return None
            exchange_pos = position_result.get("position")
            if position_result["status"] == "found":
                logger.warning(
                    f"⚠️ Posisi ditemukan di Binance tapi tidak di state! "
                    f"Syncing: {exchange_pos['side']} {exchange_pos['contracts']}"
                )
                self.state.set_position(
                    symbol=symbol,
                    side=exchange_pos["side"],
                    entry_price=exchange_pos["entry_price"],
                    amount=exchange_pos["contracts"],
                    order_id="synced_from_exchange",
                )
                return None
            
            # Setup leverage & margin
            self.setup_symbol(symbol)
            
            # Timeout durasi
            if score is not None and score >= config.HIGH_CONVICTION_SCORE:
                timeout_min = config.HIGH_CONVICTION_TIMEOUT_MINUTES
            else:
                timeout_min = config.NORMAL_CONVICTION_TIMEOUT_MINUTES
            
            # Hitung entry price: Dynamic Confluence vs Tiered Fixed Offset
            if suggested_price is not None and suggested_price > 0 and getattr(config, "DYNAMIC_PULLBACK_ENTRY_ENABLED", True):
                entry_price = float(suggested_price)
                gap_pct = abs(current_price - entry_price) / current_price * 100
                logger.info(
                    f"🎯 DYNAMIC CONFLUENCE ENTRY: {signal} {symbol} @ {entry_price} "
                    f"(Diskon: {gap_pct:.2f}% dari harga pasar {current_price}) | Timeout: {timeout_min}m"
                )
                if signal == "LONG":
                    side = "buy"
                elif signal == "SHORT":
                    side = "sell"
                else:
                    return None
            else:
                # Tentukan offset & timeout berdasarkan kualitas sinyal (Tiered Limit)
                if score is not None and score >= config.HIGH_CONVICTION_SCORE:
                    offset_pct = config.HIGH_CONVICTION_OFFSET_PERCENT
                    tier_desc = f"🔥 HIGH CONVICTION (Score {score} >= {config.HIGH_CONVICTION_SCORE})"
                else:
                    offset_pct = config.NORMAL_CONVICTION_OFFSET_PERCENT
                    score_str = str(score) if score is not None else "N/A"
                    tier_desc = f"⚖️ NORMAL CONVICTION (Score {score_str})"
                
                logger.info(f"🎯 Tiered Limit Setup: {tier_desc}")
                logger.info(f"   Offset: {offset_pct}% | Timeout: {timeout_min} min")

                offset = current_price * (offset_pct / 100)
                if signal == "LONG":
                    side = "buy"
                    entry_price = current_price - offset  # Antri dibawah
                elif signal == "SHORT":
                    side = "sell"
                    entry_price = current_price + offset  # Antri diatas
                else:
                    logger.warning(f"⚠️ Signal tidak valid: {signal}")
                    return None
            
            # Hitung jumlah kontrak
            balance = self.get_available_balance()
            if balance < config.MIN_WALLET_BALANCE_USDT:
                logger.error(f"❌ Balance {balance:.2f} USDT dibawah batas aman {config.MIN_WALLET_BALANCE_USDT} USDT.")
                return None
            
            usable_balance = balance * config.BALANCE_USAGE
            exposure_cap = usable_balance * config.LEVERAGE

            # Risk sizing memakai jarak initial hard-stop. Leverage menentukan
            # margin, tetapi kerugian harga tetap notional x stop distance.
            stop_distance_pct = float(
                (initial_stop_plan or {}).get("distance_pct")
                or getattr(config, "INITIAL_STOP_MAX_DISTANCE_PERCENT", 5.0)
            )
            if stop_distance_pct <= 0:
                stop_distance_pct = float(
                    getattr(config, "INITIAL_STOP_MAX_DISTANCE_PERCENT", 5.0)
                )
            risk_budget = balance * float(getattr(config, "RISK_PER_TRADE_PERCENT", 1.0)) / 100
            risk_notional_cap = risk_budget / (stop_distance_pct / 100)
            notional = min(exposure_cap, risk_notional_cap)
            logger.info(
                f"🛡️ Risk sizing: stop {stop_distance_pct:.2f}% | "
                f"budget ${risk_budget:.2f} | notional ${notional:.2f} "
                f"(exposure cap ${exposure_cap:.2f})"
            )
            
            # Ambil info market untuk precision
            market = self.exchange.market(symbol)
            min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
            min_notional = market.get("limits", {}).get("cost", {}).get("min", 0)
            
            # Gunakan ccxt built-in precision (lebih reliable dari round manual)
            entry_price = float(self.exchange.price_to_precision(symbol, entry_price))
            amount = notional / entry_price
            amount = float(self.exchange.amount_to_precision(symbol, amount))
            
            if amount < min_amount:
                logger.error(
                    f"❌ Amount {amount} dibawah minimum {min_amount} "
                    f"untuk {symbol}"
                )
                return None
            
            if min_notional and (amount * entry_price) < min_notional:
                logger.error(
                    f"❌ Notional {amount * entry_price:.2f} dibawah minimum "
                    f"{min_notional} untuk {symbol}"
                )
                return None
            
            # Place limit order
            logger.info(
                f"📝 Placing LIMIT {side.upper()} {symbol} | "
                f"Price: {entry_price} | Amount: {amount} | "
                f"Notional: ${notional:.2f}"
            )
            
            client_order_id = self._client_order_id(symbol)
            order_params = {"newClientOrderId": client_order_id}
            good_till_date = None
            if getattr(config, "ENTRY_GTD_ENABLED", True):
                good_till_date = (
                    int(time.time()) + (timeout_min * 60) + config.ENTRY_GTD_GRACE_SECONDS
                ) * 1000
                order_params.update({
                    "timeInForce": "GTD",
                    "goodTillDate": good_till_date,
                })

            try:
                order = self.exchange.create_order(
                    symbol=symbol,
                    type="limit",
                    side=side,
                    amount=amount,
                    price=entry_price,
                    params=order_params,
                )
            except (ccxt.NetworkError, ccxt.RequestTimeout) as create_error:
                # Binance -1007/timeout berarti outcome tidak diketahui. Jangan mengirim
                # order kedua; simpan intent dan rekonsiliasi memakai clientOrderId.
                logger.error(
                    f"❌ Outcome create order tidak diketahui ({create_error}). "
                    f"Rekonsiliasi via clientOrderId={client_order_id}."
                )
                self.state.set_pending_order(
                    symbol=symbol,
                    side=signal.lower(),
                    price=entry_price,
                    amount=amount,
                    order_id=None,
                    timeout_minutes=timeout_min,
                    client_order_id=client_order_id,
                    good_till_date=good_till_date,
                    status_unknown=True,
                    initial_stop_plan=initial_stop_plan,
                    entry_signal_snapshot=signal_context,
                )
                self.state.set_connection_status("api_error", create_error)
                return {"id": None, "clientOrderId": client_order_id, "status": "unknown"}
            
            order_id = order.get("id")
            logger.info(f"✅ Order placed! ID: {order_id}")
            
            # Simpan ke state
            self.state.set_pending_order(
                symbol=symbol,
                side=signal.lower(),
                price=entry_price,
                amount=amount,
                order_id=order_id,
                timeout_minutes=timeout_min,
                client_order_id=client_order_id,
                good_till_date=good_till_date,
                initial_stop_plan=initial_stop_plan,
                entry_signal_snapshot=signal_context,
            )
            self.refresh_entry_deadman(symbol, force=True)
            
            return order
            
        except ccxt.InsufficientFunds as e:
            logger.error(f"❌ Insufficient funds: {e}")
            return None
        except ccxt.ExchangeError as e:
            logger.error(f"❌ Exchange error placing order: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error placing order: {e}")
            return None
    
    # =========================================================================
    # Check Order Status
    # =========================================================================

    def _sync_fill_to_position(self, symbol, order, pending, clear_pending=True):
        filled_amount = float(order.get("filled") or 0)
        if filled_amount <= 0:
            return False
        filled_price = float(order.get("average") or order.get("price") or pending.get("price") or 0)
        order_id = order.get("id") or pending.get("order_id") or pending.get("client_order_id")
        self.state.sync_position(
            symbol=symbol,
            side=pending["side"],
            entry_price=filled_price,
            amount=filled_amount,
            order_id=order_id,
            initial_stop_plan=pending.get("initial_stop_plan"),
            entry_signal_snapshot=pending.get("entry_signal_snapshot"),
        )
        if clear_pending:
            self.state.clear_pending_order()
            self.disable_entry_deadman(symbol)
        return True
    
    def check_order_filled(self, symbol, order_id):
        """
        Cek apakah pending order sudah terisi.
        Handles: fully filled, partially filled, canceled, expired.
        
        Returns:
            str: 'filled', 'partial', 'open', 'canceled', atau 'error'
        """
        try:
            pending = self.state.get_pending_order()
            if not pending:
                return "canceled"
            order = self._fetch_order(
                symbol,
                order_id=order_id,
                client_order_id=pending.get("client_order_id"),
            )
            if not pending.get("order_id") and order.get("id"):
                pending["order_id"] = order["id"]
                pending["status_unknown"] = False
                self.state.save()
            status = order.get("status", "unknown")
            filled_amount = float(order.get("filled", 0))
            
            if status == "closed":
                # Order fully filled
                filled_price = order.get("average") or order.get("price", 0)
                
                logger.info(
                    f"🎯 Order FILLED! {symbol} @ {filled_price} | "
                    f"Amount: {filled_amount}"
                )
                
                self._sync_fill_to_position(symbol, order, pending, clear_pending=True)
                return "filled"
            
            elif status == "canceled" or status == "expired":
                # Cek apakah ada partial fill sebelum cancel
                if filled_amount > 0:
                    filled_price = order.get("average") or order.get("price", 0)
                    logger.warning(
                        f"⚠️ Order {order_id} canceled/expired tapi PARTIALLY FILLED! "
                        f"Filled: {filled_amount} @ {filled_price}"
                    )
                    
                    self._sync_fill_to_position(symbol, order, pending, clear_pending=True)
                    return "partial"
                else:
                    logger.info(f"🚫 Order {order_id} was {status}.")
                    self.state.clear_pending_order(start_cooldown=False)
                    self.disable_entry_deadman(symbol)
                    return "canceled"
            
            elif status == "open":
                if filled_amount > 0:
                    # Jangan biarkan sisa order terus menambah exposure. Simpan posisi
                    # parsial dahulu, lalu cancel remainder. Jika API putus, GTD/dead-man
                    # tetap membatasi umur remainder dan state menyimpan keduanya.
                    logger.warning(
                        f"⚠️ Partial fill terdeteksi: {filled_amount}. "
                        "Membatalkan remainder sebelum melanjutkan."
                    )
                    self._sync_fill_to_position(symbol, order, pending, clear_pending=False)
                    try:
                        self.exchange.cancel_order(order.get("id") or order_id, symbol)
                        final_order = self._fetch_order(
                            symbol,
                            order_id=order.get("id") or order_id,
                            client_order_id=pending.get("client_order_id"),
                        )
                        self._sync_fill_to_position(symbol, final_order, pending, clear_pending=True)
                        return "partial"
                    except ccxt.OrderNotFound:
                        # Status final akan dipastikan pada siklus berikutnya.
                        return "partial_open"
                    except Exception as e:
                        logger.error(f"❌ Gagal cancel remainder partial fill: {e}")
                        self.state.set_connection_status("api_error", e)
                        return "partial_open"
                return "open"
            
            else:
                logger.warning(f"⚠️ Unknown order status: {status}")
                return status
                
        except ccxt.OrderNotFound:
            logger.warning(
                f"⚠️ Order {order_id} not found di exchange! "
                f"Mungkin sudah expired/canceled."
            )
            position_result = self.get_position_status(symbol)
            if position_result["status"] == "found":
                pos = position_result["position"]
                self.state.sync_position(
                    symbol, pos["side"], pos["entry_price"], pos["contracts"],
                    order_id=order_id or "reconciled_fill",
                )
                self.state.clear_pending_order()
                self.disable_entry_deadman(symbol)
                return "partial"
            if position_result["status"] == "error":
                return "error"
            self.state.clear_pending_order(start_cooldown=False)
            self.disable_entry_deadman(symbol)
            return "canceled"
        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error checking order {order_id}: {e}")
            return "error"
        except Exception as e:
            logger.error(f"❌ Error checking order {order_id}: {e}")
            return "error"
    
    # =========================================================================
    # Cancel Order (dengan safety checks)
    # =========================================================================
    
    def cancel_order(self, symbol, order_id):
        """
        Cancel pending order dengan safety checks dan final partial-fill reconciliation.
        """
        pending = self.state.get_pending_order()
        client_order_id = pending.get("client_order_id") if pending else None
        try:
            order = self._fetch_order(symbol, order_id, client_order_id)
            status = order.get("status", "unknown")
            filled_amount = float(order.get("filled") or 0)

            if status == "closed":
                if pending:
                    self._sync_fill_to_position(symbol, order, pending, clear_pending=True)
                return "filled"

            if status in ("canceled", "expired", "rejected"):
                if filled_amount > 0 and pending:
                    self._sync_fill_to_position(symbol, order, pending, clear_pending=True)
                    return "partial"
                self.state.clear_pending_order(start_cooldown=True, symbol=symbol)
                self.disable_entry_deadman(symbol)
                return "canceled"

            if filled_amount > 0 and pending:
                self._sync_fill_to_position(symbol, order, pending, clear_pending=False)

            actual_order_id = order.get("id") or order_id
            if not actual_order_id:
                raise ccxt.OrderNotFound("Order id belum diketahui")
            self.exchange.cancel_order(actual_order_id, symbol)
            logger.info(f"🚫 Order {actual_order_id} canceled for {symbol}")

            final_order = self._fetch_order(symbol, actual_order_id, client_order_id)
            if float(final_order.get("filled") or 0) > 0 and pending:
                self._sync_fill_to_position(symbol, final_order, pending, clear_pending=True)
                return "partial"

            self.state.clear_pending_order(start_cooldown=True, symbol=symbol)
            self.disable_entry_deadman(symbol)
            return "canceled"
            
        except ccxt.OrderNotFound:
            logger.warning(
                f"⚠️ Order {order_id} not found saat cancel. "
                f"Checking exchange position..."
            )
            sync_result = self._sync_position_from_exchange(symbol)
            self.disable_entry_deadman(symbol)
            return sync_result
            
        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error canceling order {order_id}: {e}")
            self.state.set_connection_status("api_error", e)
            return "error"
            
        except Exception as e:
            logger.error(f"❌ Error canceling order {order_id}: {e}")
            self.state.set_connection_status("api_error", e)
            return "error"
    
    def _retry_cancel(self, symbol, order_id, attempts=0):
        """Retry cancel order dengan backoff."""
        if attempts >= self.MAX_RETRIES:
            logger.error(
                f"❌ Cancel order {order_id} GAGAL setelah {self.MAX_RETRIES}x retry!"
            )
            return False
        
        time.sleep(self.RETRY_DELAY * (attempts + 1))
        logger.info(f"🔄 Retry cancel order #{attempts + 1}...")
        
        try:
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"✅ Order {order_id} canceled (retry #{attempts + 1})")
            self.state.clear_pending_order(start_cooldown=True, symbol=symbol)
            return True
        except ccxt.OrderNotFound:
            logger.info(f"✅ Order {order_id} already gone (retry #{attempts + 1})")
            self._sync_position_from_exchange(symbol)
            return True
        except Exception as e:
            logger.warning(f"⚠️ Retry #{attempts + 1} gagal: {e}")
            return self._retry_cancel(symbol, order_id, attempts + 1)
    
    def cancel_all_orders(self, symbol):
        """Cancel SEMUA open orders (termasuk conditional TP/SL) untuk symbol."""
        success = True
        try:
            self.exchange.cancel_all_orders(symbol)
            logger.info(f"🚫 All regular orders canceled for {symbol}")
        except Exception as e:
            logger.warning(f"⚠️ Error canceling regular orders: {e}")
            success = False
        
        # Raw Binance Futures Purge (hapus semua conditional stop order seketika)
        try:
            market = self.exchange.market(symbol)
            raw_id = market.get("id", symbol.replace("/", "").split(":")[0])
            self.exchange.fapiPrivateDeleteAllOpenOrders({"symbol": raw_id})
            logger.info(f"🧹 Raw Binance regular orders purged for {raw_id}")
        except Exception:
            pass

        try:
            market = self.exchange.market(symbol)
            raw_id = market.get("id", symbol.replace("/", "").split(":")[0])
            self.exchange.fapiPrivateDeleteAlgoOpenOrders({"symbol": raw_id})
            logger.info(f"🧹 Raw Binance algo & conditional stop orders purged for {raw_id}")
        except Exception:
            pass
        
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order["id"], symbol)
                    logger.info(f"  🚫 Canceled remaining order: {order['id']} ({order.get('type')})")
                except ccxt.OrderNotFound:
                    pass
                except Exception as e:
                    logger.warning(f"  ⚠️ Failed to cancel {order['id']}: {e}")
                    success = False
        except Exception as e:
            logger.warning(f"⚠️ Error fetching open orders for cleanup: {e}")
        
        return success

    def sweep_orphaned_orders(self):
        """
        Sapu bersih semua sisa conditional / stop / limit order (termasuk Algo Orders)
        yang tidak memiliki posisi aktif di akun.
        Mencegah order Stop Loss atau TP lama tertinggal di exchange.
        """
        try:
            active_pos = self.state.get_position()
            pending = self.state.get_pending_order()
            
            allowed_symbols = set()
            if active_pos and active_pos.get("symbol"):
                allowed_symbols.add(active_pos["symbol"])
                allowed_symbols.add(active_pos["symbol"].replace("/", "").split(":")[0])
            if pending and pending.get("symbol"):
                allowed_symbols.add(pending["symbol"])
                allowed_symbols.add(pending["symbol"].replace("/", "").split(":")[0])
                
            try:
                open_orders = self.exchange.fapiPrivateGetOpenOrders()
            except Exception:
                open_orders = []
                
            cleaned_count = 0
            for o in open_orders:
                raw_sym = o.get("symbol")
                order_id = o.get("orderId")
                
                # Check jika simbol ini bukan posisi aktif atau pending order bot
                if raw_sym not in allowed_symbols and raw_sym not in [s.replace("/", "").split(":")[0] for s in allowed_symbols]:
                    try:
                        self.exchange.fapiPrivateDeleteOrder({"symbol": raw_sym, "orderId": order_id})
                        logger.info(f"🧹 Canceled leftover conditional/open order on {raw_sym}: ID {order_id} ({o.get('type')})")
                        cleaned_count += 1
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to cancel leftover order {order_id} on {raw_sym}: {e}")

            # Sapu bersih open algo orders sisa
            try:
                open_algo = self.exchange.fapiPrivateGetOpenAlgoOrders()
                for ao in open_algo:
                    raw_sym = ao.get("symbol")
                    algo_id = ao.get("algoId")
                    if raw_sym not in allowed_symbols and raw_sym not in [s.replace("/", "").split(":")[0] for s in allowed_symbols]:
                        try:
                            self.exchange.fapiPrivateDeleteAlgoOrder({"symbol": raw_sym, "algoId": int(algo_id)})
                            logger.info(f"🧹 Canceled leftover algo order on {raw_sym}: ID {algo_id}")
                            cleaned_count += 1
                        except Exception as e:
                            logger.warning(f"⚠️ Failed to cancel leftover algo order {algo_id} on {raw_sym}: {e}")
            except Exception:
                pass
                        
            if cleaned_count > 0:
                logger.info(f"✨ Total {cleaned_count} leftover conditional/open orders cleaned up from Binance.")
        except Exception as e:
            logger.warning(f"⚠️ Error during sweep_orphaned_orders: {e}")
    
    def _sync_position_from_exchange(self, symbol):
        """Sync posisi dari Binance ke local state."""
        result = self.get_position_status(symbol)
        if result["status"] == "found":
            pos = result["position"]
            try:
                logger.warning(
                    f"🔄 SYNC: Posisi ditemukan di exchange! "
                    f"{pos['side']} {pos['contracts']} @ {pos['entry_price']}"
                )
                pending = self.state.get_pending_order()
                side = pending["side"] if pending else pos["side"]
                
                self.state.sync_position(
                    symbol=symbol,
                    side=side,
                    entry_price=pos["entry_price"],
                    amount=pos["contracts"],
                    order_id="synced_from_exchange",
                )
                self.state.clear_pending_order()
                return "partial"
            except Exception as e:
                logger.error(f"❌ Error syncing position: {e}")
                return "error"
        if result["status"] == "empty":
            self.state.clear_pending_order(start_cooldown=True, symbol=symbol)
            return "canceled"
        logger.error("❌ Sync posisi ditunda karena API_ERROR; state lokal dipertahankan.")
        return "error"

    @staticmethod
    def _order_identity(order):
        info = order.get("info") or {}
        order_id = order.get("id") or info.get("orderId")
        client_id = order.get("clientOrderId") or info.get("clientOrderId")
        return str(order_id) if order_id is not None else None, client_id

    def _is_bot_owned_order(self, order):
        _, client_id = self._order_identity(order)
        return bool(client_id and str(client_id).startswith(config.BOT_ORDER_CLIENT_PREFIX))

    def reconcile_startup(self):
        """
        Jadikan Binance sumber kebenaran sebelum scanning dimulai.

        Tidak pernah membatalkan order yang tidak cocok dengan state lokal atau
        prefix clientOrderId milik bot.
        """
        logger.info("🔄 Startup reconciliation: positions, entries, dan stop protection...")
        if not self.recover_exit_intent():
            return {"status": "error", "error": "Exit intent unresolved; preserve exchange protection and reconcile client ID"}
        try:
            raw_positions = self.exchange.fetch_positions()
            positions = []
            for pos in raw_positions:
                contracts = abs(float(pos.get("contracts", 0) or 0))
                if contracts <= 0:
                    continue
                positions.append({
                    "symbol": pos.get("symbol"),
                    "side": str(pos.get("side") or "").lower(),
                    "contracts": contracts,
                    "entry_price": float(pos.get("entryPrice", 0) or 0),
                })
            open_orders = self._fetch_all_open_orders()
            open_algo_response = self.exchange.fapiPrivateGetOpenAlgoOrders()
            open_algos = self._algo_rows(open_algo_response)
        except Exception as e:
            logger.critical(f"🚨 Startup reconciliation gagal: {e}")
            self.state.set_connection_status("api_error", e)
            self.state.mark_reconciled("api_error")
            return {"status": "error", "error": str(e)}

        if len(positions) > config.MAX_POSITIONS:
            message = f"Ditemukan {len(positions)} posisi aktif; batas bot {config.MAX_POSITIONS}."
            logger.critical(f"🚨 {message} Tidak ada order yang diubah.")
            self.state.set_status("reconciliation_blocked")
            self.state.mark_reconciled("multiple_positions")
            return {"status": "error", "error": message}

        local_pending = self.state.get_pending_order()
        local_position = self.state.get_position()

        def matches_pending(order):
            if not local_pending:
                return False
            oid, cid = self._order_identity(order)
            return (
                (local_pending.get("order_id") is not None and oid == str(local_pending.get("order_id")))
                or (
                    local_pending.get("client_order_id")
                    and cid == local_pending.get("client_order_id")
                )
            )

        matching_open = next((o for o in open_orders if matches_pending(o)), None)

        if not positions:
            if local_position:
                # Posisi ditutup manual/oleh stop ketika bot offline. Jangan menganggap
                # kegagalan API sebagai flat: seluruh fetch di atas sudah sukses.
                remaining = float(local_position.get("amount") or 0)
                estimated = self.get_realized_pnl(
                    local_position["symbol"], local_position["side"],
                    float(local_position["entry_price"]), remaining,
                ) if remaining > 0 else 0.0
                self.state.clear_position(
                    pnl=estimated,
                    reason="startup_exchange_flat",
                    start_cooldown=False,
                    record_history=True,
                )

            if local_pending:
                if matching_open:
                    oid, _ = self._order_identity(matching_open)
                    if oid and not local_pending.get("order_id"):
                        local_pending["order_id"] = oid
                        local_pending["status_unknown"] = False
                        self.state.save()
                    self.refresh_entry_deadman(local_pending["symbol"], force=True)
                    self.state.set_connection_status("connected")
                    self.state.mark_reconciled("pending_entry_restored")
                    return {"status": "ok", "action": "pending_restored"}

                # Tidak open: query status final bila memungkinkan. Jika tidak ditemukan
                # dan exchange juga flat, intent lokal aman untuk dibersihkan.
                try:
                    final_order = self._fetch_order(
                        local_pending["symbol"],
                        local_pending.get("order_id"),
                        local_pending.get("client_order_id"),
                    )
                    if float(final_order.get("filled") or 0) > 0:
                        logger.warning(
                            "⚠️ Entry lama sempat terisi tetapi exchange sekarang flat; "
                            "diasumsikan sudah ditutup manual."
                        )
                except ccxt.OrderNotFound:
                    pass
                except Exception as e:
                    self.state.set_connection_status("api_error", e)
                    self.state.mark_reconciled("pending_status_unknown")
                    return {"status": "error", "error": str(e)}
                self.state.clear_pending_order(start_cooldown=False)
                self.disable_entry_deadman(local_pending["symbol"])

            # Recover bot-owned entry yang ada di exchange tetapi state hilang.
            bot_entries = [o for o in open_orders if self._is_bot_owned_order(o)]
            if len(bot_entries) > 1:
                message = "Lebih dari satu pending entry bot ditemukan; perlu pemeriksaan manual."
                self.state.set_status("reconciliation_blocked")
                self.state.mark_reconciled("multiple_pending_entries")
                return {"status": "error", "error": message}
            if len(bot_entries) == 1:
                order = bot_entries[0]
                oid, cid = self._order_identity(order)
                side = "long" if str(order.get("side", "")).lower() == "buy" else "short"
                self.state.set_pending_order(
                    symbol=order["symbol"],
                    side=side,
                    price=float(order.get("price") or 0),
                    amount=float(order.get("amount") or 0),
                    order_id=oid,
                    client_order_id=cid,
                )
                self.refresh_entry_deadman(order["symbol"], force=True)
                self.state.mark_reconciled("bot_entry_recovered")
                return {"status": "ok", "action": "pending_recovered"}

            self.state.state["trailing_stop"] = None
            self.state.state["protection_status"] = "none"
            self.state.set_connection_status("connected")
            self.state.mark_reconciled("flat")
            return {"status": "ok", "action": "flat"}

        position = positions[0]
        symbol = position["symbol"]
        if not symbol:
            message = "Posisi aktif tidak memiliki symbol yang dapat dipetakan."
            self.state.mark_reconciled("invalid_position")
            return {"status": "error", "error": message}

        self.state.sync_position(
            symbol=symbol,
            side=position["side"],
            entry_price=position["entry_price"],
            amount=position["contracts"],
            order_id=(local_position or {}).get("order_id", "startup_reconciled"),
        )

        if local_pending and matching_open:
            cancel_result = self.cancel_order(
                local_pending["symbol"], local_pending.get("order_id")
            )
            if cancel_result == "error":
                self.state.mark_reconciled("partial_remainder_unknown")
                return {"status": "error", "error": "Gagal membatalkan remainder entry"}
        elif local_pending:
            self.state.clear_pending_order(start_cooldown=False)
            self.disable_entry_deadman(local_pending["symbol"])

        raw_symbol = self._raw_symbol(symbol)
        active_stops = []
        for algo in open_algos:
            status = str(algo.get("algoStatus") or algo.get("status") or "").upper()
            order_type = str(algo.get("orderType") or algo.get("type") or "").upper()
            if (
                algo.get("symbol") == raw_symbol
                and status in ("NEW", "ACTIVE")
                and "STOP" in order_type
            ):
                active_stops.append(algo)

        if active_stops:
            local_stop_id = (self.state.get_trailing_stop() or {}).get("order_id")
            adopted = self.deduplicate_stop_algos(
                symbol, position["side"], preferred_id=local_stop_id
            )
            stop_id = adopted["id"]
            stop_price = adopted["stop_price"]
            stop_amount = adopted.get("amount") or position["contracts"]
            checkpoint = (self.state.get_trailing_stop() or {}).get("checkpoint_level", 0)
            self.state.set_trailing_stop(
                stop_id, stop_price, checkpoint, amount=stop_amount
            )
            action = "position_and_stop_recovered"
        else:
            self.state.state["trailing_stop"] = None
            self.state.set_protection_status("missing")
            action = "position_requires_stop"

        self.state.set_connection_status("connected")
        self.state.mark_reconciled(action)
        return {"status": "ok", "action": action, "position": position}
    
    # =========================================================================
    # Realized PnL & Close Position
    # =========================================================================
    
    def get_realized_pnl(self, symbol, side, entry_price, amount, fallback_close_price=None, close_order_id=None):
        """
        Dapatkan PnL riil dari Binance API berdasarkan trades history (menjumlahkan seluruh fills dan memotong fee komisi).
        Jika tidak tersedia, gunakan fallback calculation dari harga close riil dikurangi estimasi fee.
        """
        try:
            trades = self.exchange.fetch_my_trades(symbol, limit=20)
            if trades:
                def _calc_net_pnl(trade_batch):
                    gross_pnl = sum(float(t.get("info", {}).get("realizedPnl", 0)) for t in trade_batch)
                    total_fee = sum(
                        float(t.get("fee", {}).get("cost", 0)) if (t.get("fee") and t.get("fee", {}).get("cost") is not None)
                        else float(t.get("info", {}).get("commission", 0))
                        for t in trade_batch
                    )
                    net_pnl = gross_pnl - total_fee
                    return gross_pnl, total_fee, net_pnl

                # 1. Jika close_order_id diketahui, cari semua fill dari order tersebut
                if close_order_id:
                    matching_trades = [
                        t for t in trades 
                        if str(t.get("order")) == str(close_order_id) or 
                           str(t.get("info", {}).get("orderId")) == str(close_order_id)
                    ]
                    if matching_trades:
                        gross, fee, net = _calc_net_pnl(matching_trades)
                        logger.info(
                            f"📊 Binance Realized PnL (Close Order #{close_order_id}, {len(matching_trades)} fills): "
                            f"Gross {gross:+.4f} | Fee -{fee:.4f} | Net: {net:+.4f} USDT"
                        )
                        return round(net, 4)

                # 2. Jika close_order_id tidak ada (misal closed on exchange / SL triggered), cari batch order penutupan terakhir
                close_side = "sell" if side == "long" else "buy"
                closing_trades = [
                    t for t in trades 
                    if not close_order_id and t.get("side") == close_side and float(t.get("info", {}).get("realizedPnl", 0)) != 0
                ]
                if closing_trades:
                    last_close_order_id = closing_trades[-1].get("order") or closing_trades[-1].get("info", {}).get("orderId")
                    if last_close_order_id:
                        batch_trades = [
                            t for t in closing_trades 
                            if (t.get("order") == last_close_order_id or t.get("info", {}).get("orderId") == last_close_order_id)
                        ]
                        gross, fee, net = _calc_net_pnl(batch_trades)
                        logger.info(
                            f"📊 Binance Realized PnL (Last Close Batch #{last_close_order_id}, {len(batch_trades)} fills): "
                            f"Gross {gross:+.4f} | Fee -{fee:.4f} | Net: {net:+.4f} USDT"
                        )
                        return round(net, 4)

                # 3. Fallback ke harga trade terakhir jika realizedPnl tidak ditemukan
                trade_price = float(trades[-1].get("price", 0))
                if not close_order_id and trade_price > 0:
                    fallback_close_price = trade_price
        except Exception as e:
            logger.warning(f"⚠️ Gagal fetch trades dari exchange: {e}")
            
        # Fallback calculation dengan estimasi fee roundtrip
        if fallback_close_price is None or fallback_close_price <= 0:
            fallback_close_price = self.get_current_price(symbol)
            
        if fallback_close_price > 0 and entry_price > 0 and amount > 0:
            if side == "long":
                gross_fallback_pnl = (fallback_close_price - entry_price) * amount
            else:
                gross_fallback_pnl = (entry_price - fallback_close_price) * amount
                
            fee_rate = getattr(config, "ESTIMATED_ROUNDTRIP_FEE_PERCENT", 0.08) / 100.0
            estimated_fee = (amount * entry_price) * fee_rate
            net_fallback_pnl = gross_fallback_pnl - estimated_fee
            logger.info(
                f"📊 Fallback Calculated PnL: Gross {gross_fallback_pnl:+.4f} | "
                f"Est. Fee -{estimated_fee:.4f} | Net: {net_fallback_pnl:+.4f} USDT"
            )
            return round(net_fallback_pnl, 4)
                
        return 0.0

    def reduce_position(self, symbol, side, amount, reason="partial_exit", flag=None):
        """Kurangi posisi dengan reduce-only market tanpa mengakhiri trade."""
        if self.state.get_exit_intent():
            self.recover_exit_intent()
            return None
        pos = self.state.get_position()
        if not pos or amount <= 0:
            return None
        amount = min(float(amount), float(pos.get("amount", 0)))
        amount = float(self.exchange.amount_to_precision(symbol, amount))
        if amount <= 0:
            return None
        close_side = "sell" if side == "long" else "buy"
        try:
            client_id = "bf_exit_" + uuid.uuid4().hex[:24]
            self.state.begin_exit_intent({
                "client_id": client_id, "symbol": symbol, "side": side,
                "amount": amount, "before_amount": float(pos["amount"]),
                "entry_price": float(pos["entry_price"]), "reason": reason,
                "entry_order_id": pos.get("order_id"),
                "flag": flag, "created_ms": int(time.time() * 1000),
            })
            order = self.exchange.create_order(
                symbol=symbol, type="market", side=close_side, amount=amount,
                params={"reduceOnly": True, "newClientOrderId": client_id},
            )
            return order if self.recover_exit_intent() else None
        except Exception as e:
            logger.error(f"Gagal partial exit {symbol}: {e}")
            self.state.set_connection_status("api_error", e)
            return None

    def recover_exit_intent(self):
        """Query the original client ID, NEVER resubmit an ambiguous partial exit."""
        intent = self.state.get_exit_intent()
        if not intent:
            return True
        try:
            order = self._fetch_order(intent["symbol"], client_order_id=intent["client_id"])
            if order["status"] not in ("closed", "canceled", "expired", "rejected"):
                return False
            filled = float(order.get("filled") or 0)
            if filled < 0 or filled > intent["amount"] + 1e-9:
                raise ValueError("Unexpected exit fill quantity")
            price = float(order.get("average") or 0)
            if filled > 0 and price <= 0:
                return False
            pnl = 0.0
            if filled > 0:
                pnl = self.get_realized_pnl(
                    intent["symbol"], intent["side"], intent["entry_price"], filled,
                    fallback_close_price=price, close_order_id=order["id"],
                )
            self.state.finish_exit_intent(intent["client_id"], order, pnl)
            return True
        except Exception as exc:
            # Even OrderNotFound can mean a delayed/ambiguous submission.
            self.state.set_connection_status("api_error", exc)
            logger.error("Exit intent unresolved; no resubmission: %s", exc)
            return False

    def close_position(self, symbol, side, amount, reason="manual"):
        """
        Close posisi aktif via MARKET order.
        Dengan retry mechanism jika gagal.
        """
        if self.state.get_exit_intent():
            self.recover_exit_intent()
            return None
        for attempt in range(self.MAX_RETRIES):
            try:
                close_side = "sell" if side == "long" else "buy"
                
                logger.info(
                    f"🔴 Closing {side.upper()} {symbol} | "
                    f"Amount: {amount} | Reason: {reason} "
                    f"(attempt {attempt + 1})"
                )
                
                order = self.exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=close_side,
                    amount=amount,
                    params={"reduceOnly": True},
                )
                
                close_order_id = order.get("id") if order else None
                # An order acknowledgement is not proof that the position is flat.
                actual_result = self.get_position_status(symbol)
                if actual_result["status"] != "empty":
                    self.state.set_connection_status("api_error")
                    logger.error("Close not confirmed flat; preserving position and exchange stops.")
                    return None
                close_price = order.get("average") or order.get("price", 0)
                if not close_price or float(close_price) <= 0:
                    close_price = self.get_current_price(symbol)
                logger.info(f"✅ Position closed @ {close_price}")
                
                # Hitung PnL riil (mengagregasi seluruh fills dari order penutupan)
                pos = self.state.get_position()
                pnl = 0.0
                if pos:
                    entry = pos["entry_price"]
                    pnl = self.get_realized_pnl(
                        symbol=symbol,
                        side=side,
                        entry_price=entry,
                        amount=amount,
                        fallback_close_price=float(close_price) if close_price else None,
                        close_order_id=close_order_id
                    )
                
                # Cancel semua remaining orders (trailing stop, dll)
                self.cancel_all_orders(symbol)
                
                # Update state (ini otomatis mengaktifkan cooldown 30m)
                self.state.clear_position(pnl=pnl, reason=reason, start_cooldown=True)
                
                logger.info(f"💰 Realized PnL: {pnl:+.2f} USDT")
                
                return order
                
            except ccxt.InsufficientFunds:
                logger.warning(
                    f"⚠️ Insufficient funds to close. "
                    f"Fetching actual position size..."
                )
                actual_result = self.get_position_status(symbol)
                actual_pos = actual_result.get("position")
                if actual_result["status"] == "found":
                    amount = actual_pos["contracts"]
                    logger.info(f"  🔄 Retrying with actual amount: {amount}")
                    continue
                elif actual_result["status"] == "empty":
                    logger.info("  ✅ Position already closed (mungkin kena stop)")
                    self.cancel_all_orders(symbol)
                    pos = self.state.get_position()
                    pnl = 0.0
                    if pos:
                        pnl = self.get_realized_pnl(
                            symbol=symbol,
                            side=side,
                            entry_price=pos["entry_price"],
                            amount=pos["amount"]
                        )
                    self.state.clear_position(pnl=pnl, reason=f"{reason}_already_closed", start_cooldown=True)
                    return None
                else:
                    logger.error("❌ Tidak dapat memastikan posisi; state lokal dipertahankan.")
                    continue
                    
            except ccxt.NetworkError as e:
                # Execution may have succeeded. Never retry blindly in this call.
                self.state.set_connection_status("api_error")
                logger.error(f"Ambiguous close response; reconcile before retry: {e}")
                return None
                    
            except ccxt.ExchangeError as e:
                error_msg = str(e).lower()
                if "position side does not match" in error_msg or \
                   "reduce only" in error_msg:
                    if self.get_position_status(symbol)["status"] != "empty":
                        self.state.set_connection_status("api_error")
                        logger.error("Close rejected with position not confirmed empty; retaining stops.")
                        return None
                    logger.warning(f"⚠️ Position mungkin sudah closed: {e}")
                    self.cancel_all_orders(symbol)
                    pos = self.state.get_position()
                    pnl = 0.0
                    if pos:
                        pnl = self.get_realized_pnl(
                            symbol=symbol,
                            side=side,
                            entry_price=pos["entry_price"],
                            amount=pos["amount"]
                        )
                    self.state.clear_position(pnl=pnl, reason=f"{reason}_already_closed", start_cooldown=True)
                    return None
                else:
                    logger.error(f"❌ Exchange error closing position: {e}")
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(self.RETRY_DELAY)
                        continue
                        
            except Exception as e:
                logger.error(f"❌ Unexpected error closing position: {e}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)
                    continue
        
        logger.critical(
            f"🚨 CRITICAL: Gagal close position setelah {self.MAX_RETRIES}x! "
            f"{side.upper()} {symbol} amount={amount}. MANUAL ACTION REQUIRED!"
        )
        return None
    
    # =========================================================================
    # Stop Market Order (untuk trailing stop ratchet)
    # =========================================================================
    
    def place_stop_order(self, symbol, side, amount, stop_price):
        """
        Place STOP_MARKET order (digunakan oleh trailing manager).
        """
        with self._stop_lock:
            try:
                stop_side = "sell" if side == "long" else "buy"
                stop_price_rounded = float(self.exchange.price_to_precision(symbol, stop_price))

                try:
                    existing = self.find_matching_stop_algo(
                        symbol, side, amount, stop_price_rounded
                    )
                    if existing:
                        logger.warning(f"Existing identical stop adopted: {existing['id']}")
                        if getattr(config, "STOP_DEDUPLICATION_ENABLED", True):
                            self.deduplicate_stop_algos(
                                symbol, side, preferred_id=existing["id"]
                            )
                        return existing
                except Exception:
                    pass
                
                logger.info(
                    f"🛡️ Placing STOP_MARKET {stop_side.upper()} {symbol} | "
                    f"Stop: {stop_price_rounded} | Amount: {amount}"
                )
                
                order = self.exchange.create_order(
                    symbol=symbol,
                    type="STOP_MARKET",
                    side=stop_side,
                    amount=amount,
                    price=None,
                    params={
                        "stopPrice": stop_price_rounded,
                        "reduceOnly": True,
                        "clientAlgoId": self._client_order_id(symbol),
                    },
                )
                
                order_id = order.get("id") or (order.get("info") or {}).get("algoId")
                if order_id is not None and not order.get("id"):
                    order["id"] = str(order_id)
                logger.info(f"✅ Stop order placed! ID: {order_id}")
                return order
            
            except ccxt.NetworkError as e:
                logger.warning(f"⚠️ Outcome create stop tidak diketahui: {e}")
                # Jangan retry create secara buta. Query stop identik untuk
                # recovery agar timeout tidak menghasilkan duplicate order.
                time.sleep(self.RETRY_DELAY)
                try:
                    recovered = self.find_matching_stop_algo(
                        symbol, side, amount, stop_price_rounded
                    )
                    if recovered:
                        logger.info(f"Stop recovered after timeout: {recovered['id']}")
                        return recovered
                except Exception:
                    pass
                return None
                    
            except ccxt.ExchangeError as e:
                error_msg = str(e).lower()
                if "would immediately trigger" in error_msg:
                    logger.warning(
                        f"⚠️ Stop price {stop_price_rounded} sudah terlewati! "
                        f"Harga sudah melewati level stop."
                    )
                    return None
                logger.error(f"❌ Exchange error placing stop: {e}")
                return None
                
            except Exception as e:
                logger.error(f"❌ Error placing stop order: {e}")
                return None
        
    def cancel_stop_order(self, symbol, order_id):
        """Cancel stop order dengan pengecekan apakah sudah triggered (mendukung Binance Algo Order)."""
        if not order_id:
            return "not_found"

        market_sym = symbol.replace("/", "").split(":")[0]

        # 1. Cek & cancel via Binance Algo Order API (karena STOP_MARKET di Futures pakai algoId)
        try:
            algo_info = self.exchange.fapiPrivateGetAlgoOrder({"algoId": int(order_id)})
            if algo_info:
                status = algo_info.get("algoStatus", "").upper()
                if status in ("FINISHED", "TRIGGERED"):
                    logger.warning(
                        f"⚠️ Stop order {order_id} sudah TRIGGERED ({status})! "
                        f"Posisi mungkin sudah ter-close."
                    )
                    return "triggered"
                elif status in ("CANCELLED", "EXPIRED", "REJECTED"):
                    logger.info(f"✅ Stop order {order_id} sudah {status}")
                    return "canceled"
                elif status in ("NEW", "ACTIVE"):
                    del_res = self.exchange.fapiPrivateDeleteAlgoOrder({
                        "symbol": market_sym,
                        "algoId": int(order_id)
                    })
                    logger.info(f"🚫 Algo stop order {order_id} canceled: {del_res.get('msg')}")
                    return "canceled"
        except ccxt.OrderNotFound:
            pass
        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error checking algo stop {order_id}: {e}")
            return "error"
        except ccxt.ExchangeError as e:
            message = str(e).lower()
            if "not found" not in message and "does not exist" not in message and "-2013" not in message:
                logger.error(f"❌ Exchange error checking algo stop {order_id}: {e}")
                return "error"
        except Exception as e:
            logger.error(f"❌ Error checking algo stop {order_id}: {e}")
            return "error"

        # 2. Fallback ke standard order API
        try:
            try:
                order = self.exchange.fetch_order(order_id, symbol)
                status = order.get("status", "unknown")
                
                if status == "closed":
                    logger.warning(
                        f"⚠️ Stop order {order_id} sudah TRIGGERED! "
                        f"Posisi mungkin sudah ter-close."
                    )
                    return "triggered"
                
                elif status == "canceled" or status == "expired":
                    logger.info(f"✅ Stop order {order_id} sudah {status}")
                    return "canceled"
                    
            except ccxt.OrderNotFound:
                pass
            
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"🚫 Stop order {order_id} canceled")
            return "canceled"
            
        except ccxt.OrderNotFound:
            logger.info(f"✅ Stop order {order_id} already gone")
            return "not_found"
        except Exception as e:
            logger.error(f"❌ Error canceling stop order {order_id}: {e}")
            return "error"
    
    # =========================================================================
    # Get Current Position & Price
    # =========================================================================
    
    def get_position_status(self, symbol):
        """Return tri-state: found, empty, atau error. API error bukan posisi kosong."""
        try:
            positions = self.exchange.fetch_positions([symbol])
            for pos in positions:
                contracts = abs(float(pos.get("contracts", 0) or 0))
                if contracts > 0:
                    normalized = {
                        "symbol": pos.get("symbol") or symbol,
                        "side": str(pos.get("side") or "").lower(),
                        "contracts": contracts,
                        "entry_price": float(pos.get("entryPrice", 0) or 0),
                        "mark_price": float(pos.get("markPrice", 0) or 0),
                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                        "leverage": pos.get("leverage"),
                        "margin_mode": pos.get("marginMode"),
                    }
                    self.state.set_connection_status("connected")
                    return {"status": "found", "position": normalized, "error": None}
            self.state.set_connection_status("connected")
            return {"status": "empty", "position": None, "error": None}
        except Exception as e:
            logger.error(f"❌ Error fetching position for {symbol}: {e}")
            self.state.set_connection_status("api_error", e)
            return {"status": "error", "position": None, "error": str(e)}

    def fetch_position(self, symbol):
        """Compatibility helper. Gunakan get_position_status untuk keputusan state."""
        result = self.get_position_status(symbol)
        return result["position"] if result["status"] == "found" else None
    
    def get_current_price(self, symbol):
        """Ambil harga terkini untuk symbol."""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker.get("last", 0)
        except Exception as e:
            logger.error(f"❌ Error fetching price for {symbol}: {e}")
            return 0
