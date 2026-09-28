"""
Reversal Guard
==============
Otomatis close posisi saat signal berbalik arah.
- LONG → Signal berubah ke SHORT → Close LONG
- SHORT → Signal berubah ke LONG → Close SHORT
- Signal WAIT/Netral → TIDAK close (biarkan trailing stop bekerja)
"""

import time
from runtime_config import config
from logger_setup import logger


class ReversalGuard:
    """Monitor signal reversal dan auto-close posisi."""
    
    def __init__(self, signal_engine, order_manager, trailing_manager, state_manager):
        self.signal_engine = signal_engine
        self.order_mgr = order_manager
        self.trailing_mgr = trailing_manager
        self.state = state_manager
        self.last_check_time = 0
    
    def should_check(self):
        """
        Cek apakah sudah waktunya cek reversal.
        Interval dikontrol oleh REVERSAL_CHECK_INTERVAL.
        """
        now = time.time()
        if now - self.last_check_time >= config.REVERSAL_CHECK_INTERVAL:
            self.last_check_time = now
            return True
        return False
    
    def check_and_act(self):
        """
        Cek reversal signal dan close posisi jika perlu.
        
        Returns:
            dict: {
                "action": "none" | "closed",
                "reason": str,
                "details": dict
            }
        """
        result = {"action": "none", "reason": "", "details": {}}
        
        if not self.should_check():
            return result
        
        pos = self.state.get_position()
        if not pos:
            return result
        
        symbol = pos["symbol"]
        side = pos["side"]
        
        logger.info(f"🔄 Checking signal reversal for {symbol} ({side.upper()})...")
        
        # Cek reversal menggunakan signal engine
        reversal = self.signal_engine.check_reversal(symbol, side)
        
        if reversal["reversed"]:
            logger.warning(
                f"⚠️ SIGNAL REVERSAL CONFIRMED for {symbol}! "
                f"{reversal['signal']} | Details: {reversal['details']}"
            )
            
            if reversal.get("severity") == "reduce":
                if pos.get("defensive_reduction_done"):
                    return result
                reduce_amount = float(pos["amount"]) * float(
                    getattr(config, "REVERSAL_PARTIAL_CLOSE_PERCENT", 50)
                ) / 100
                reduced = self.trailing_mgr.reduce_and_resize_stop(
                    symbol, side, reduce_amount,
                    reason=f"defensive_reversal:{reversal['signal']}",
                    flag="defensive_reduction_done",
                    lock_profit_pct=None,
                )
                if reduced:
                    result.update({
                        "action": "reduced", "reason": reversal["signal"],
                        "details": reversal["details"],
                    })
                return result

            # Market-close dahulu sementara hard-stop exchange tetap aktif.
            # close_position membersihkan stop hanya setelah close mendapat ACK.
            close_result = self.order_mgr.close_position(
                symbol=symbol,
                side=side,
                amount=pos["amount"],
                reason=f"signal_reversal: {reversal['signal']}",
            )
            
            if close_result:
                result["action"] = "closed"
                result["reason"] = reversal["signal"]
                result["details"] = reversal["details"]
                logger.info(
                    f"🔴 Position CLOSED by reversal guard | "
                    f"{reversal['signal']} | {reversal['details']}"
                )
            else:
                logger.error("❌ Failed to close position on reversal!")
        else:
            logger.info(f"  ✅ No reversal - position is safe")
        
        return result
