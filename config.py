"""
Binance Futures Trading Bot - Configuration
============================================
Semua konfigurasi utama bot ada di sini.
"""

import os
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
LEVERAGE = 3                    # Leverage 3x
MARGIN_MODE = "isolated"        # Isolated margin (lebih aman per posisi)
BALANCE_USAGE = 0.35            # Gunakan 35% balance (sisakan 65% buffer)
MAX_POSITIONS = 1               # Hanya 1 posisi aktif
MIN_WALLET_BALANCE_USDT = 5.0   # Saldo minimum untuk bot mulai trading

# =============================================================================
# COOLDOWN & ANTI-REENTRY SETTINGS
# =============================================================================
SIGNAL_COOLDOWN_MINUTES = 30            # Cooldown global 30 menit setelah trade selesai
SYMBOL_COOLDOWN_MINUTES = 60            # Cooldown khusus koin yang sama (60 menit)
TIMEOUT_COOLDOWN_MINUTES = 1            # Cooldown global jika limit order cancel/timeout (hanya 1m sebelum scan koin lain)
MAX_CONSECUTIVE_LOSSES = 3              # Max loss berturut-turut sebelum cooldown panjang
DOUBLE_COOLDOWN_AFTER_LOSSES = 2        # Double cooldown (60m) jika kena 2 loss berturut-turut
CONSECUTIVE_LOSS_PAUSE_MINUTES = 120    # Cooldown maksimal losestreak 2 jam (auto-resume setelah 2 jam)

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

# =============================================================================
# PRO TRAILING STOP CONFIGURATION (Sweet Spot Breathing Room Ratchet)
# =============================================================================
# Checkpoint 1: Profit +2.0% (ROE +6%) → Kunci BEP di +0.3% (Ruang napas 1.7% anti-kejilat)
# Checkpoint 2: Profit +3.5% (ROE +10.5%) → Geser stop ke +1.5% (Kunci bersih ~$4.5)
# Checkpoint 3: Profit +5.0% (ROE +15%) → Geser stop ke +3.0% (Kunci bersih ~$9.0)
TRAILING_FIRST_CHECKPOINT_PERCENT = 2.0 # Checkpoint pertama di +2.0% (ROE +6.0% di 3x)
TRAILING_FIRST_STOP_PERCENT = 0.3       # Kunci BEP + fee (+0.3%)
TRAILING_CHECKPOINT_STEP = 1.5          # Step checkpoint berikutnya (+3.5%, +5.0%, +6.5%, dst)
TRAILING_STOP_OFFSET = 2.0              # Jarak kawal stop 2% di bawah checkpoint (ruang bernapas ideal)

# =============================================================================
# TIME-DELAYED BEP & STAGNATION TIME-STOP (Anti-Sideways Protection)
# =============================================================================
TIME_DELAYED_BEP_ENABLED = True             # Aktifkan gembok BEP otomatis setelah waktu tertentu
TIME_DELAYED_BEP_MINUTES = 120              # Aktif setelah posisi berjalan 2 jam (8 candle 15m) agar tidak kejilat retest
TIME_DELAYED_BEP_MIN_PROFIT_PERCENT = 0.8   # Syarat: Pernah/sedang profit >= +0.8% (ROE +2.4% di 3x)
TIME_DELAYED_BEP_STOP_PERCENT = 0.22        # Kunci stop loss di +0.22% (Cover fee Binance 0.08% + Net Profit Bersih)

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
SIGNAL_MIN_SCORE = 75           # Skor minimum sinyal untuk entry (0-100)

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

# Volume
VOLUME_SMA_PERIOD = 20
MIN_VOLUME_RATIO = 0.8          # Minimal volume 0.8x dari average 20 SMA

# Timeframes & Confirmation
TRADING_TIMEFRAME = "15m"       # Timeframe utama (15m cepat & responsif)
HIGHER_TIMEFRAME = "1h"         # Timeframe konfirmasi trend makro (1h)
CANDLE_HISTORY_CONFIRMATION_COUNT = 3

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
SCANNER_TOP_N = 50              # Scan top 50 koin berdasarkan volume
MIN_24H_CHANGE_PERCENT = 1.0    # Minimum perubahan harga 24h (absolute)
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
