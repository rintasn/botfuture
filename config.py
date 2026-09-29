"""
Binance Futures Trading Bot - Configuration
============================================
Semua konfigurasi utama bot ada di sini.
"""

import os
import json
import hashlib
import math
import threading
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# API CONFIGURATION
# =============================================================================
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TRADING_MODE = os.getenv("TRADING_MODE", "testnet")  # 'live' atau 'testnet'

# =============================================================================
# TRADING PARAMETERS
# =============================================================================
LEVERAGE = 2                    # Leverage 2x
MARGIN_MODE = "isolated"        # Isolated margin (lebih aman per posisi)
BALANCE_USAGE = 0.25            # Gunakan 25% balance (sisakan 75% buffer)
MAX_POSITIONS = 1               # Hanya 1 posisi aktif
MIN_WALLET_BALANCE_USDT = 35.0   # Saldo minimum untuk bot mulai trading

# =============================================================================
# COOLDOWN & ANTI-REENTRY SETTINGS
# =============================================================================
SIGNAL_COOLDOWN_MINUTES = 20            # Cooldown global 20 menit setelah trade selesai
SYMBOL_COOLDOWN_MINUTES = 60            # Cooldown khusus koin yang sama (60 menit)
TIMEOUT_COOLDOWN_MINUTES = 1            # Cooldown global jika limit order cancel/timeout (hanya 1m sebelum scan koin lain)
MAX_CONSECUTIVE_LOSSES = 3              # Max loss berturut-turut sebelum cooldown panjang
DOUBLE_COOLDOWN_AFTER_LOSSES = 1        # Double cooldown dasar mulai loss pertama
CONSECUTIVE_LOSS_PAUSE_MINUTES = 30     # Pause losestreak 30 menit

# =============================================================================
# ORDER SETTINGS (TIERED HYBRID LIMIT ORDER)
# =============================================================================
# Level 1: Sinyal Kuat (High Conviction - Score >= 80)
HIGH_CONVICTION_SCORE = 80              # Ambang batas sinyal super kuat
HIGH_CONVICTION_OFFSET_PERCENT = 0.1    # Limit order sangat rapat (0.1%) agar cepat fill
HIGH_CONVICTION_TIMEOUT_MINUTES = 10    # Timeout 10 menit

# Level 2: Sinyal Standar (Normal Conviction - Score 70-79)
NORMAL_CONVICTION_OFFSET_PERCENT = 0.35 # Limit order tawar sehat (0.35%)
NORMAL_CONVICTION_TIMEOUT_MINUTES = 15  # Timeout 15 menit

# Default fallback / backward compatibility
ENTRY_OFFSET_PERCENT = 0.35             # Default limit offset
ORDER_TIMEOUT_MINUTES = 15              # Default timeout
ORDER_CHECK_INTERVAL = 10               # Cek order setiap 10 detik

# Exchange-side expiry + kill switch untuk pending entry. GTD tetap bekerja
# ketika proses bot/laptop mati; countdownCancelAll diperbarui selama entry
# masih pending dan hanya dipasang pada simbol entry tersebut.
ENTRY_GTD_ENABLED = True
ENTRY_GTD_GRACE_SECONDS = 15             # Binance mensyaratkan GTD > now + 600 detik
ENTRY_DEADMAN_ENABLED = True
ENTRY_DEADMAN_COUNTDOWN_MS = 120_000     # Rekomendasi Binance: 120 detik
ENTRY_DEADMAN_REFRESH_SECONDS = 30
BOT_ORDER_CLIENT_PREFIX = "bf_"
PENDING_REVALIDATION_SECONDS = 30       # Batalkan limit jika setup closed-candle sudah invalid

# =============================================================================
# PRO TRAILING STOP CONFIGURATION (Sweet Spot Breathing Room Ratchet)
# =============================================================================
# Checkpoint 1: Profit +2.0% (ROE +6%) → Kunci BEP di +0.3% (Ruang napas 1.7% anti-kejilat)
# Checkpoint 2: Profit +3.5% (ROE +10.5%) → Geser stop ke +1.5% (Kunci bersih ~$4.5)
# Checkpoint 3: Profit +5.0% (ROE +15%) → Geser stop ke +3.0% (Kunci bersih ~$9.0)
TRAILING_FIRST_CHECKPOINT_PERCENT = 2.0 # Checkpoint pertama di +2.0% harga
TRAILING_FIRST_STOP_PERCENT = 0.3       # Kunci BEP + fee (+0.3%)
TRAILING_CHECKPOINT_STEP = 1.5          # Step checkpoint berikutnya (+3.5%, +5.0%, +6.5%, dst)
TRAILING_STOP_OFFSET = 2.0              # Jarak kawal stop 2% di bawah checkpoint (ruang bernapas ideal)

# =============================================================================
# TIME-DELAYED BEP & STAGNATION TIME-STOP (Anti-Sideways Protection)
# =============================================================================
TIME_DELAYED_BEP_ENABLED = True             # Aktifkan gembok BEP otomatis setelah waktu tertentu
TIME_DELAYED_BEP_MINUTES = 120              # Aktif setelah posisi berjalan 2 jam (8 candle 15m) agar tidak kejilat retest
TIME_DELAYED_BEP_MIN_PROFIT_PERCENT = 0.8   # Syarat: pernah/sedang profit harga >= +0.8%
TIME_DELAYED_BEP_STOP_PERCENT = 0.22        # Kunci stop loss di +0.22% (Cover fee Binance 0.08% + Net Profit Bersih)

# Profit defense berbasis initial risk (R). TP dijalankan bot dengan reduce-only
# market order, sedangkan hard-stop exchange tetap melindungi sisa posisi.
PARTIAL_TP_ENABLED = True
TP1_R_MULTIPLE = 1.0
TP1_MAX_PRICE_PERCENT = 1.0
TP1_CLOSE_PERCENT = 50.0
TP2_R_MULTIPLE = 2.0
TP2_MAX_PRICE_PERCENT = 2.0
TP2_CLOSE_PERCENT = 25.0               # Persen dari ukuran awal
EARLY_BEP_ENABLED = True
EARLY_BEP_TRIGGER_PERCENT = 0.8
EARLY_BEP_STOP_PERCENT = 0.22

# Posisi yang tidak pernah membuktikan momentum tidak dibiarkan terlalu lama.
TIME_STOP_ENABLED = True
TIME_STOP_MINUTES = 90
TIME_STOP_MAX_MFE_R = 0.5

# Dynamic Time-Progressive Ratchet: Semakin lama posisi berjalan, kerek stop semakin naik
# agar tidak terjebak menunggu lama hanya untuk keluar di 0 koma sekian persen.
TIME_PROGRESSIVE_LOCK_ENABLED = True
TIME_PROGRESSIVE_TIERS = [
    # (elapsed_minutes, min_profit_req_pct, lock_stop_pct)
    (90,  1.4, 0.8),   # 1.5 Jam: Jika pernah/sedang profit >= +1.4%, kunci stop minimal +0.8% (ROE +2.4% -> ~$2.0-$2.5)
    (120, 1.7, 1.2),   # 2.0 Jam: Jika pernah/sedang profit >= +1.7%, kunci stop minimal +1.2% (ROE +3.6% -> ~$3.0-$3.5)
    (150, 2.0, 1.6),   # 2.5 Jam: Jika pernah/sedang profit >= +2.0%, kunci stop minimal +1.6% (ROE +4.8% -> ~$4.0-$4.5)
    (180, 2.2, 1.9),   # 3.0 Jam: Jika pernah/sedang profit >= +2.2%, kunci stop minimal +1.9% (ROE +5.7% -> ~$5.0+)
]

# Stagnation Force Close: Dinonaktifkan agar tidak memotong tren yang sedang bagus.
# Posisi dibiarkan berjalan (let winners run) sepenuhnya dikawal oleh Trailing Stop (by Price & by Time).
STAGNATION_EXIT_ENABLED = False
MAX_STAGNANT_HOURS = 3.0
MAX_STAGNANT_MIN_PROFIT_PERCENT = 0.2

# =============================================================================
# DYNAMIC CONFLUENCE ENTRY (EMA 21 & ATR Pullback)
# =============================================================================
DYNAMIC_PULLBACK_ENTRY_ENABLED = True       # Entry di level support/resistance dinamis
ATR_PULLBACK_MULTIPLIER = 0.35              # Jarak pullback berbasis ATR (0.35x ATR)
ESTIMATED_ROUNDTRIP_FEE_PERCENT = 0.08      # Estimasi total fee round-trip Binance (Maker 0.02% + Taker 0.05% + buffer)

# Backward compatibility
TRAILING_CHECKPOINT_PERCENT = 2.0

# =============================================================================
# ADAPTIVE INITIAL HARD STOP & RISK CAP
# =============================================================================
# Stop awal tetap berada di Binance agar posisi terlindungi ketika bot/laptop
# offline. Level dasarnya mengikuti struktur 15m (EMA55 + swing + ATR buffer),
# lalu dijepit agar tidak terlalu dekat maupun terlalu jauh dari entry.
EMERGENCY_SL_ENABLED = True
INITIAL_STOP_ATR_MULTIPLIER = 1.5
INITIAL_STOP_ATR_BUFFER = 0.25
INITIAL_STOP_SWING_LOOKBACK = 20
INITIAL_STOP_MIN_DISTANCE_PERCENT = 1.0
INITIAL_STOP_MAX_DISTANCE_PERCENT = 5.0

# Maksimum kerugian teoritis pada initial stop, dalam persen dari available
# wallet. Notional tetap dibatasi juga oleh BALANCE_USAGE x LEVERAGE.
RISK_PER_TRADE_PERCENT = 1.0

# Alias kompatibilitas untuk kode/state lama. Nilainya sekarang adalah batas
# maksimum adaptive stop, bukan fixed stop yang selalu dipakai.
EMERGENCY_SL_PERCENT = INITIAL_STOP_MAX_DISTANCE_PERCENT
MANDATORY_STOP_PROTECTION = True
FAIL_CLOSE_IF_STOP_UNPROTECTED = True

# Binance Algo Order dapat belum terlihat sesaat setelah create berhasil.
# Selama grace period ini order dianggap aktif berdasarkan ACK exchange.
STOP_VERIFICATION_GRACE_SECONDS = 8
STOP_DEDUPLICATION_ENABLED = True

# =============================================================================
# SIGNAL ENGINE SETTINGS (Institutional TPLR)
# =============================================================================
ENTRY_STRATEGY = "TPLR"          # TPLR atau SMC_EMA; hanya satu aktif per siklus
SIGNAL_MIN_SCORE = 85           # Entry dibuat lebih selektif; skor kini dimulai dari 0
NEUTRAL_REGIME_MIN_SCORE = 92   # Hanya setup luar biasa yang boleh lolos regime netral
ENTRY_REQUIRE_4H_ALIGNMENT = True
ENTRY_SKIP_NEUTRAL_BTC = True
ENTRY_MIN_REJECTION_CLOSE_LOCATION = 0.65
ENTRY_MIN_15M_VOLUME_RATIO = 1.0
ENTRY_MAX_EMA21_DISTANCE_ATR = 1.0
ENTRY_MAX_LIVE_DISTANCE_ATR = 1.5
ENTRY_CONFIRM_TIMEFRAME = "4h"

# SMC-EMA: zona swing yang diikuti displacement, retest, lalu break struktur.
# Semua struktur dihitung dari candle 15m yang sudah tutup.
SMC_LOOKBACK_CANDLES = 48
SMC_PIVOT_BARS = 2
SMC_DISPLACEMENT_BARS = 6
SMC_DISPLACEMENT_ATR = 1.0
SMC_MAX_ZONE_WIDTH_ATR = 0.8
SMC_RETEST_BARS = 5
SMC_BREAK_BARS = 3
SMC_MIN_VOLUME_RATIO = 1.0
SMC_MIN_REWARD_RISK = 1.5
SMC_STOP_BUFFER_ATR = 0.25
SMC_MAX_ENTRY_CHASE_ATR = 0.75

# EMA Parameters
EMA_FAST = 21
EMA_SLOW = 55
EMA_TREND = 200

# MACD Parameters
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# RSI Parameters
RSI_PERIOD = 14
RSI_OVERBOUGHT = 65
RSI_OVERSOLD = 35
RSI_REVERSAL_HIGH = 75
RSI_REVERSAL_LOW = 25

# ADX Parameters
ADX_PERIOD = 14
ADX_THRESHOLD = 25
ADX_STRONG_THRESHOLD = 30

# Volume
VOLUME_SMA_PERIOD = 20
MIN_VOLUME_RATIO = 0.8          # Minimal volume 0.8x dari average 20 SMA

# Timeframes & Confirmation
TRADING_TIMEFRAME = "15m"       # Timeframe utama (15m cepat & responsif)
HIGHER_TIMEFRAME = "1h"         # Timeframe konfirmasi trend makro (1h)
CANDLE_HISTORY_CONFIRMATION_COUNT = 3
USE_CLOSED_CANDLES_ONLY = True

# Regime harga BTC absolut melengkapi BTC.D (yang hanya mengukur relative strength).
BTC_MARKET_FILTER_ENABLED = True
BTC_MARKET_SYMBOL = "BTC/USDT:USDT"
BTC_MARKET_TIMEFRAME = "1h"
BTC_MARKET_CACHE_SECONDS = 60

# =============================================================================
# BTC DOMINANCE MARKET REGIME FILTER
# =============================================================================
# BTCDOMUSDT menjadi proxy relative strength BTC terhadap altcoin besar.
# Filter ini hanya mengatur entry/exit altcoin, bukan arah absolut harga BTC.
BTCDOM_FILTER_ENABLED = True
BTCDOM_SYMBOL = "BTCDOM/USDT:USDT"
BTCDOM_TIMEFRAME = "1h"
BTCDOM_EXIT_TIMEFRAME = "15m"
BTCDOM_CACHE_SECONDS = 60
BTCDOM_STRICT_ENTRY_FILTER = True
BTCDOM_ALIGNED_SCORE_BONUS = 5
BTCDOM_EXIT_ON_REVERSAL = True
BTCDOM_EXIT_CONFIRMATION_CANDLES = 2

# =============================================================================
# SCANNER SETTINGS
# =============================================================================
SCANNER_TOP_N = 300              # Scan top 300 koin berdasarkan volume
MIN_24H_CHANGE_PERCENT = 1.0    # Minimum perubahan harga 24h (absolute)
MIN_QUOTE_VOLUME_USDT = 10_000_000
MAX_SPREAD_PERCENT = 0.05       # Maximum spread yang diperbolehkan
CRYPTO_ONLY_SCANNER = True
CRYPTO_UNDERLYING_TYPES = ("COIN",)  # Binance USD-M metadata; TradFi/index ditolak

# Blacklist koin (stablecoins, TradFi perps, low liquidity, dll)
BLACKLIST_COINS = [
    # Stablecoins
    "USDC/USDT", "BUSD/USDT", "TUSD/USDT", "DAI/USDT",
    "USDP/USDT", "FDUSD/USDT", "USDD/USDT",
    # TradFi Perpetuals (butuh agreement terpisah)
    "SAMSUNG/USDT", "SNDK/USDT", "CL/USDT", "SKHYNIX/USDT",
    "SOXL/USDT", "MU/USDT", "AKE/USDT", "MSTR/USDT",
    "XAU/USDT", "AIN/USDT", "KORU/USDT", "SPCX/USDT",
    "SKHY/USDT", "SNXX/USDT", "POWER/USDT", "PONS/USDT",
    "CRCL/USDT", "BR/USDT", "BZ/USDT", "XAG/USDT",
    # Market regime index (filter only, tidak ditradingkan oleh bot)
    "BTCDOM/USDT",
]

# =============================================================================
# REVERSAL GUARD
# =============================================================================
REVERSAL_CHECK_INTERVAL = 30    # Cek reversal setiap 30 detik
REVERSAL_PARTIAL_CLOSE_PERCENT = 50.0

# Circuit breaker berdasarkan hasil aktual, bukan hanya jumlah loss berturut-turut.
RISK_CIRCUIT_ENABLED = True
DAILY_MAX_LOSS_R = 2.0
ROLLING_RISK_WINDOW = 10
ROLLING_MIN_TRADES = 6
ROLLING_MIN_PROFIT_FACTOR = 0.8
RISK_CIRCUIT_PAUSE_MINUTES = 360
MEANINGFUL_WIN_R = 0.25

# =============================================================================
# DASHBOARD
# =============================================================================
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DASHBOARD_HOST = "0.0.0.0"

# =============================================================================
# LOGGING
# =============================================================================
LOG_DIR = "logs"
LOG_LEVEL = "INFO"

# =============================================================================
# STATE PERSISTENCE
# =============================================================================
STATE_FILE = "bot_state.json"

# =============================================================================
# MAIN LOOP
# =============================================================================
MAIN_LOOP_INTERVAL = 10         # Sleep 10 detik antar iterasi
CANDLE_FETCH_LIMIT = 250        # Jumlah candle yang di-fetch untuk analisis

# User-data WebSocket (CCXT Pro tersedia di paket ccxt yang sama).
USER_STREAM_ENABLED = True
USER_STREAM_RECONNECT_MAX_SECONDS = 30

# =============================================================================
# DASHBOARD AUTH & RUNTIME STRATEGY SETTINGS
# =============================================================================
# Kredensial default sesuai deployment saat ini. Untuk deployment berikutnya,
# environment variable tetap dapat mengganti nilai tanpa mengubah source code.
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "qais")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", r"User\@mis1")
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY") or hashlib.sha256(
    f"{BINANCE_API_SECRET}:{DASHBOARD_PASSWORD}:dashboard-session".encode("utf-8")
).hexdigest()
RUNTIME_CONFIG_FILE = os.getenv("RUNTIME_CONFIG_FILE", os.path.join(os.path.dirname(__file__), "strategy_overrides.json"))
RUNTIME_LOCK = threading.RLock()
TP_PRICE_CAP_ENABLED = True

# Hanya parameter berikut yang dapat diubah dari admin panel. API credentials,
# TRADING_MODE, MAX_POSITIONS, dan proteksi wajib sengaja tidak diekspos.
ADMIN_EDITABLE_CONFIG = {
    "ENTRY_STRATEGY": {"type": "choice", "choices": ("TPLR", "SMC_EMA"), "category": "Entry", "label": "Entry strategy"},
    "TP_PRICE_CAP_ENABLED": {"type": "bool", "category": "Exit", "label": "Cap TP price (OFF = pure R)"},
    "ENTRY_REQUIRE_4H_ALIGNMENT": {"type": "bool", "category": "Entry", "label": "Require 4h trend alignment"},
    "ENTRY_SKIP_NEUTRAL_BTC": {"type": "bool", "category": "Entry", "label": "Skip alt entries while BTC regime neutral"},
    "ENTRY_MIN_REJECTION_CLOSE_LOCATION": {"type": "float", "min": 0.55, "max": 0.9, "step": 0.05, "category": "Entry", "label": "Rejection candle close location"},
    "ENTRY_MIN_15M_VOLUME_RATIO": {"type": "float", "min": 0.8, "max": 2.0, "step": 0.1, "category": "Entry", "label": "Entry candle volume / average"},
    "ENTRY_MAX_EMA21_DISTANCE_ATR": {"type": "float", "min": 0.25, "max": 2.0, "step": 0.05, "category": "Entry", "label": "Max EMA21 distance in ATR"},
    "ENTRY_MAX_LIVE_DISTANCE_ATR": {"type": "float", "min": 0.5, "max": 3.0, "step": 0.1, "category": "Entry", "label": "Max live price distance from EMA21 in ATR"},
    "MIN_QUOTE_VOLUME_USDT": {"type": "int", "min": 1000000, "max": 1000000000, "step": 1000000, "category": "Entry", "label": "Minimum 24h quote volume USDT"},
    "BALANCE_USAGE": {"type": "float", "min": 0.05, "max": 0.50, "step": 0.01, "category": "Risk", "label": "Balance usage"},
    "RISK_PER_TRADE_PERCENT": {"type": "float", "min": 0.10, "max": 2.00, "step": 0.05, "category": "Risk", "label": "Risk per trade (%)"},
    "INITIAL_STOP_ATR_MULTIPLIER": {"type": "float", "min": 0.50, "max": 3.00, "step": 0.10, "category": "Risk", "label": "Initial stop ATR multiplier"},
    "INITIAL_STOP_MIN_DISTANCE_PERCENT": {"type": "float", "min": 0.30, "max": 3.00, "step": 0.10, "category": "Risk", "label": "Minimum stop distance (%)"},
    "INITIAL_STOP_MAX_DISTANCE_PERCENT": {"type": "float", "min": 1.00, "max": 8.00, "step": 0.25, "category": "Risk", "label": "Maximum stop distance (%)"},
    "SIGNAL_MIN_SCORE": {"type": "int", "min": 70, "max": 100, "step": 1, "category": "Entry", "label": "Minimum signal score"},
    "NEUTRAL_REGIME_MIN_SCORE": {"type": "int", "min": 80, "max": 100, "step": 1, "category": "Entry", "label": "Neutral regime minimum score"},
    "ADX_THRESHOLD": {"type": "float", "min": 15, "max": 45, "step": 1, "category": "Entry", "label": "ADX threshold"},
    "ADX_STRONG_THRESHOLD": {"type": "float", "min": 20, "max": 60, "step": 1, "category": "Entry", "label": "Strong ADX threshold"},
    "MIN_VOLUME_RATIO": {"type": "float", "min": 0.50, "max": 3.00, "step": 0.10, "category": "Entry", "label": "Minimum volume ratio"},
    "MIN_24H_CHANGE_PERCENT": {"type": "float", "min": 0.20, "max": 10.00, "step": 0.10, "category": "Scanner", "label": "Minimum 24h change (%)"},
    "MAX_SPREAD_PERCENT": {"type": "float", "min": 0.01, "max": 0.50, "step": 0.01, "category": "Scanner", "label": "Maximum spread (%)"},
    "SCANNER_TOP_N": {"type": "int", "min": 20, "max": 500, "step": 10, "category": "Scanner", "label": "Markets scanned"},
    "SIGNAL_COOLDOWN_MINUTES": {"type": "int", "min": 1, "max": 240, "step": 1, "category": "Execution", "label": "Global cooldown (minutes)"},
    "SYMBOL_COOLDOWN_MINUTES": {"type": "int", "min": 1, "max": 720, "step": 1, "category": "Execution", "label": "Symbol cooldown (minutes)"},
    "PENDING_REVALIDATION_SECONDS": {"type": "int", "min": 10, "max": 300, "step": 5, "category": "Execution", "label": "Pending revalidation (seconds)"},
    "PARTIAL_TP_ENABLED": {"type": "bool", "category": "Exit", "label": "Enable partial TP"},
    "TP1_R_MULTIPLE": {"type": "float", "min": 0.50, "max": 3.00, "step": 0.10, "category": "Exit", "label": "TP1 R multiple"},
    "TP1_MAX_PRICE_PERCENT": {"type": "float", "min": 0.30, "max": 5.00, "step": 0.10, "category": "Exit", "label": "TP1 price cap (%)"},
    "TP1_CLOSE_PERCENT": {"type": "float", "min": 10, "max": 80, "step": 5, "category": "Exit", "label": "TP1 close size (%)"},
    "TP2_R_MULTIPLE": {"type": "float", "min": 1.00, "max": 5.00, "step": 0.10, "category": "Exit", "label": "TP2 R multiple"},
    "TP2_MAX_PRICE_PERCENT": {"type": "float", "min": 0.50, "max": 10.00, "step": 0.10, "category": "Exit", "label": "TP2 price cap (%)"},
    "TP2_CLOSE_PERCENT": {"type": "float", "min": 5, "max": 60, "step": 5, "category": "Exit", "label": "TP2 close size (%)"},
    "EARLY_BEP_ENABLED": {"type": "bool", "category": "Exit", "label": "Enable early BEP"},
    "EARLY_BEP_TRIGGER_PERCENT": {"type": "float", "min": 0.20, "max": 5.00, "step": 0.10, "category": "Exit", "label": "Early BEP trigger (%)"},
    "EARLY_BEP_STOP_PERCENT": {"type": "float", "min": 0.05, "max": 2.00, "step": 0.01, "category": "Exit", "label": "Early BEP lock (%)"},
    "TIME_STOP_ENABLED": {"type": "bool", "category": "Exit", "label": "Enable time stop"},
    "TIME_STOP_MINUTES": {"type": "int", "min": 15, "max": 480, "step": 15, "category": "Exit", "label": "Time stop (minutes)"},
    "REVERSAL_PARTIAL_CLOSE_PERCENT": {"type": "float", "min": 10, "max": 100, "step": 5, "category": "Exit", "label": "Soft reversal close (%)"},
    "BTCDOM_FILTER_ENABLED": {"type": "bool", "category": "Regime", "label": "Enable BTC.D filter"},
    "BTC_MARKET_FILTER_ENABLED": {"type": "bool", "category": "Regime", "label": "Enable BTC market filter"},
    "RISK_CIRCUIT_ENABLED": {"type": "bool", "category": "Circuit Breaker", "label": "Enable risk circuit"},
    "DAILY_MAX_LOSS_R": {"type": "float", "min": 0.50, "max": 10.00, "step": 0.25, "category": "Circuit Breaker", "label": "Daily maximum loss (R)"},
    "ROLLING_MIN_PROFIT_FACTOR": {"type": "float", "min": 0.20, "max": 2.00, "step": 0.05, "category": "Circuit Breaker", "label": "Rolling minimum profit factor"},
    "RISK_CIRCUIT_PAUSE_MINUTES": {"type": "int", "min": 30, "max": 1440, "step": 30, "category": "Circuit Breaker", "label": "Circuit pause (minutes)"},
}


def _coerce_admin_value(name, value):
    """Validasi nilai admin terhadap whitelist dan batas aman."""
    meta = ADMIN_EDITABLE_CONFIG[name]
    kind = meta["type"]
    if kind == "choice":
        parsed = str(value).upper()
        if parsed not in meta["choices"]:
            raise ValueError(f"{name} harus salah satu dari {', '.join(meta['choices'])}")
        return parsed
    if kind == "bool":
        if isinstance(value, bool):
            parsed = value
        elif str(value).lower() in ("true", "1", "on", "yes"):
            parsed = True
        elif str(value).lower() in ("false", "0", "off", "no"):
            parsed = False
        else:
            raise ValueError(f"{name} harus boolean")
    elif kind == "int":
        number = float(value)
        if not math.isfinite(number) or not number.is_integer():
            raise ValueError(f"{name} harus bilangan bulat finite")
        parsed = int(number)
    else:
        parsed = float(value)
    if kind != "bool":
        if not math.isfinite(parsed) or parsed < meta["min"] or parsed > meta["max"]:
            raise ValueError(f"{name} harus antara {meta['min']} dan {meta['max']}")
    return parsed


def apply_runtime_overrides(values):
    """Apply validated values ke module config dan cek relasi antar-field."""
    parsed = {}
    for name, value in values.items():
        if name not in ADMIN_EDITABLE_CONFIG:
            raise ValueError(f"Parameter tidak diizinkan: {name}")
        parsed[name] = _coerce_admin_value(name, value)
    prospective = {name: parsed.get(name, globals()[name]) for name in ADMIN_EDITABLE_CONFIG}
    if prospective["INITIAL_STOP_MIN_DISTANCE_PERCENT"] > prospective["INITIAL_STOP_MAX_DISTANCE_PERCENT"]:
        raise ValueError("Minimum stop distance tidak boleh melebihi maximum stop distance")
    if prospective["ADX_THRESHOLD"] > prospective["ADX_STRONG_THRESHOLD"]:
        raise ValueError("ADX threshold tidak boleh melebihi strong ADX threshold")
    if prospective["TP1_CLOSE_PERCENT"] + prospective["TP2_CLOSE_PERCENT"] > 90:
        raise ValueError("Total TP1 + TP2 tidak boleh melebihi 90% ukuran awal")
    if prospective["TP2_R_MULTIPLE"] <= prospective["TP1_R_MULTIPLE"] or prospective["TP2_MAX_PRICE_PERCENT"] <= prospective["TP1_MAX_PRICE_PERCENT"]:
        raise ValueError("Target TP2 harus lebih tinggi daripada TP1")
    if prospective["EARLY_BEP_STOP_PERCENT"] >= prospective["EARLY_BEP_TRIGGER_PERCENT"]:
        raise ValueError("BEP lock harus di bawah BEP trigger")
    for name, value in parsed.items():
        globals()[name] = value
    return parsed


def load_runtime_overrides():
    """Load override strategi persisten; file rusak tidak boleh menggagalkan startup."""
    if not os.path.exists(RUNTIME_CONFIG_FILE):
        return {}
    try:
        with open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as handle:
            values = json.load(handle)
        return apply_runtime_overrides(values)
    except Exception as exc:
        print(f"WARNING: strategy overrides diabaikan: {exc}")
        return {}


load_runtime_overrides()
