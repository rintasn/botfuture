"""Offline, fixed-entry exit-policy comparison. NOT a full strategy backtest.

Input: JSON list of {symbol, side, regime, entry_time (epoch ms), entry_price,
risk_pct, amount, candles: [{time, open, high, low, close, funding_rate?}]}.
Candles must begin strictly after entry; funding_rate is a signed rate applied
once at its funding event (not repeated on every candle).
"""
import argparse
import json
import math


def simulate(episode, capped=True, fee_bps=5, slippage_bps=3):
    side = episode["side"]
    if side not in ("long", "short"):
        raise ValueError("side must be long/short")
    direction = 1 if side == "long" else -1
    entry = float(episode["entry_price"])
    amount = float(episode["amount"])
    risk = float(episode["risk_pct"])
    if not all(math.isfinite(x) and x > 0 for x in (entry, amount, risk)) or risk >= 100:
        raise ValueError("Invalid entry, amount or risk")
    if not all(math.isfinite(x) and x >= 0 for x in (fee_bps, slippage_bps)):
        raise ValueError("Costs must be nonnegative and finite")
    remaining = amount
    fee = fee_bps / 10000
    slip = slippage_bps / 10000
    pnl = -amount * entry * (fee + slip)
    stop = entry * (1 - direction * risk / 100)
    tp1 = min(risk, 1) if capped else risk
    tp2 = min(2 * risk, 2) if capped else 2 * risk
    done1 = done2 = False
    mfe = 0.0
    last_time = int(episode["entry_time"])
    candles = episode["candles"]
    if not candles:
        raise ValueError("Missing candles")

    def close(qty, price):
        nonlocal remaining, pnl
        qty = min(qty, remaining)
        executed = price * (1 - direction * slip)
        pnl += qty * ((executed - entry) * direction - executed * fee)
        remaining -= qty

    # Validate the ENTIRE path, including bars after a simulated exit.
    for bar in candles:
        stamp = int(bar["time"])
        values = [float(bar[k]) for k in ("open", "high", "low", "close")]
        o, h, l, c = values
        funding = float(bar.get("funding_rate", 0))
        if stamp <= last_time or not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError("Unordered candles or invalid OHLC")
        if not l <= min(o, c) <= max(o, c) <= h or not math.isfinite(funding):
            raise ValueError("Invalid OHLC/funding")
        last_time = stamp

    for bar in candles:
        if remaining <= 1e-12:
            break
        o, h, l, c = [float(bar[k]) for k in ("open", "high", "low", "close")]
        # Funding charged on opening quantity; conservative if stop also hits.
        pnl -= direction * remaining * o * float(bar.get("funding_rate", 0))
        if (direction == 1 and l <= stop) or (direction == -1 and h >= stop):
            close(remaining, min(o, stop) if direction == 1 else max(o, stop))
            break
        profit = direction * (c - entry) / entry * 100
        mfe = max(mfe, profit)
        if not done1 and profit >= tp1:
            close(amount * 0.5, c)
            done1 = True
        elif done1 and not done2 and profit >= tp2:
            close(amount * 0.25, c)
            done2 = True
        lock = None
        if profit >= 0.8 or done1:
            lock = 0.22
        if profit >= 2:
            checkpoint = 2 + int((profit - 2) // 1.5) * 1.5
            lock = max(lock or 0, 0.3 if checkpoint == 2 else checkpoint - 2)
        if lock is not None:
            target = entry * (1 + direction * lock / 100)
            stop = max(stop, target) if direction == 1 else min(stop, target)
        elapsed = (int(bar["time"]) - int(episode["entry_time"])) / 60000
        if elapsed >= 90 and mfe < risk * 0.5:
            close(remaining, c)
    if remaining > 0:
        close(remaining, float(candles[-1]["close"]))
    return {"pnl": pnl, "r": pnl / (entry * amount * risk / 100)}


def summary(results):
    if not results:
        return {"trades": 0, "pnl": 0, "mean_r": None}
    return {"trades": len(results), "pnl": sum(r["pnl"] for r in results),
            "mean_r": sum(r["r"] for r in results) / len(results)}


def walk_forward(episodes, train=30, test=10, fee_bps=5, slippage_bps=3):
    if train < 1 or test < 1:
        raise ValueError("train/test must be positive")
    data = sorted(episodes, key=lambda e: int(e["entry_time"]))
    results = {mode: [simulate(e, mode == "capped", fee_bps, slippage_bps) for e in data]
               for mode in ("capped", "pure_r")}
    folds, heldout = [], []
    for split in range(train, len(data), test):
        # Purge overlapping outcomes: training must have ENDED before test entry.
        indices = [i for i in range(split - train, split)
                   if int(data[i]["candles"][-1]["time"]) < int(data[split]["entry_time"])]
        if not indices:
            continue
        chosen = max(results, key=lambda mode: sum(results[mode][i]["r"] for i in indices) / len(indices))
        end = min(split + test, len(data))
        batch = results[chosen][split:end]
        heldout.extend((data[i], results[chosen][i]) for i in range(split, end))
        folds.append({"train_trades_after_purge": len(indices), "test_start": data[split]["entry_time"],
                      "selected": chosen, "out_of_sample": summary(batch)})
    grouped = {}
    for episode, result in heldout:
        key = f"{episode['side']}:{episode.get('regime', 'unknown')}"
        grouped.setdefault(key, []).append(result)
    return {"model": "fixed-entry, close-only decisions; stop-first OHLC; no entry/reversal simulation",
            "fee_bps": fee_bps, "slippage_bps": slippage_bps,
            "comparison_not_oos": {k: summary(v) for k, v in results.items()},
            "folds": folds, "out_of_sample": summary([r for _, r in heldout]),
            "oos_by_side_regime": {k: summary(v) for k, v in grouped.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Local episode JSON (never connects to exchange)")
    parser.add_argument("--train", type=int, default=30)
    parser.add_argument("--test", type=int, default=10)
    parser.add_argument("--fee-bps", type=float, default=5)
    parser.add_argument("--slippage-bps", type=float, default=3)
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as handle:
        data = json.load(handle)
    print(json.dumps(walk_forward(data, args.train, args.test, args.fee_bps, args.slippage_bps), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
