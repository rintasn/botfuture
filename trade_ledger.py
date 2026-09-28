"""Read-only lifecycle accounting. Never equate incomplete history with zero costs."""
from datetime import datetime
import math


class TradeLedger:
    LIMIT = 1000
    MAX_PAGES = 50

    def __init__(self, exchange):
        self.exchange = exchange

    def reconcile(self, trade):
        symbol = trade["symbol"]
        raw_symbol = self.exchange.market(symbol)["id"]
        end = int(datetime.fromisoformat(trade["close_time"]).timestamp() * 1000)
        entry_id = trade.get("order_id")
        if not entry_id:
            raise ValueError("Missing entry order ID; lifecycle cannot be attributed")
        anchor = self.exchange.fapiPrivateGetUserTrades({
            "symbol": raw_symbol, "orderId": entry_id, "limit": self.LIMIT,
        })
        anchor = [t for t in anchor if str(t["orderId"]) == str(entry_id)]
        if not anchor or len(anchor) >= self.LIMIT:
            raise ValueError("Entry fills missing or truncated")
        start = min(int(t["time"]) for t in anchor)
        if end < start:
            raise ValueError("Invalid lifecycle timestamps")
        # fromId pagination preserves fills sharing the same millisecond.
        cursor = min(int(t["id"]) for t in anchor)
        fills = {}
        for _ in range(self.MAX_PAGES):
            batch = self.exchange.fapiPrivateGetUserTrades({
                "symbol": raw_symbol, "fromId": cursor, "limit": self.LIMIT,
            })
            for fill in batch:
                if start <= int(fill["time"]) <= end:
                    fills[str(fill["id"])] = fill
            if len(batch) < self.LIMIT or any(int(t["time"]) > end for t in batch):
                break
            next_id = max(int(t["id"]) for t in batch) + 1
            if next_id <= cursor:
                raise ValueError("Fill pagination stalled")
            cursor = next_id
        else:
            raise ValueError("Fill pagination limit exceeded")
        if not fills or not all(str(t["id"]) in fills for t in anchor):
            raise ValueError("Incomplete entry fills")
        entry_side = "BUY" if trade["side"] == "long" else "SELL"
        opens = [t for t in fills.values() if t["side"] == entry_side]
        closes = [t for t in fills.values() if t["side"] != entry_side]
        if any(str(t["orderId"]) != str(entry_id) for t in opens):
            raise ValueError("Other entry order overlaps lifecycle; manual attribution required")
        opened = sum(float(t["qty"]) for t in opens)
        closed = sum(float(t["qty"]) for t in closes)
        if opened <= 0 or not math.isclose(opened, closed, rel_tol=1e-8, abs_tol=1e-10):
            raise ValueError("Lifecycle fills not flat yet")

        gross = sum(float(t["realizedPnl"]) for t in fills.values())
        fees = 0.0
        conversions = []
        for fill in fills.values():
            cost = float(fill["commission"])
            asset = fill["commissionAsset"]
            if cost == 0:
                continue
            if asset == "USDT":
                fees += cost
                continue
            timestamp = int(fill["time"]) // 60000 * 60000
            bars = self.exchange.fetch_ohlcv(f"{asset}/USDT", "1m", since=timestamp, limit=1)
            if not bars or int(bars[0][0]) != timestamp or float(bars[0][4]) <= 0:
                raise ValueError(f"Missing historical commission conversion: {asset}")
            rate = float(bars[0][4])
            fees += cost * rate
            conversions.append({"asset": asset, "amount": cost, "rate": rate,
                                "timestamp": timestamp, "method": "1m_close_estimate"})

        funding_rows = {}
        # Income API has bounded time windows; paginate each 6-day segment.
        window = start
        while window <= end:
            window_end = min(end, window + 6 * 86400000 - 1)
            for page in range(1, self.MAX_PAGES + 1):
                batch = self.exchange.fapiPrivateGetIncome({
                    "symbol": raw_symbol, "incomeType": "FUNDING_FEE",
                    "startTime": window, "endTime": window_end,
                    "limit": self.LIMIT, "page": page,
                })
                for row in batch:
                    if row.get("symbol") != raw_symbol or row.get("incomeType") != "FUNDING_FEE":
                        raise ValueError("Unexpected funding scope")
                    if row["asset"] != "USDT":
                        raise ValueError("Non-USDT funding requires explicit conversion")
                    funding_rows[str(row["tranId"])] = row
                if len(batch) < self.LIMIT:
                    break
            else:
                raise ValueError("Funding pagination limit exceeded")
            window = window_end + 1
        funding = sum(float(t["income"]) for t in funding_rows.values())
        pnl = gross - fees + funding
        if not all(math.isfinite(v) for v in (gross, fees, funding, pnl)):
            raise ValueError("Non-finite ledger values")
        return {"pnl": pnl, "gross_pnl": gross, "fees_usdt": fees, "funding_usdt": funding,
                "fill_ids": sorted(fills), "funding_ids": sorted(funding_rows),
                "fx_conversions": conversions, "start_ms": start, "end_ms": end,
                "status": "estimated_fx" if conversions else "reconciled"}
