# Binance Futures Trading Bot

Bot trading otomatis untuk Binance USDT-M Futures dengan strategi **Trend-Pullback & Liquidity Rejection (TPLR)**, satu posisi aktif, limit entry dinamis, serta proteksi posisi melalui adaptive exchange hard-stop dan trailing-stop ratchet.

> Status proyek: tahap pengembangan. Hardening recovery dan order lifecycle sudah diimplementasikan, tetapi tetap wajib divalidasi cukup lama di testnet sebelum memakai dana riil.

## Fitur utama

| Fitur | Implementasi saat ini |
|---|---|
| Single position | Satu posisi atau satu pending order dalam state bot |
| Binance Futures | USDT perpetual futures melalui CCXT |
| Leverage dan margin | Fixed 2x, isolated margin |
| Market scanner | Hanya underlying crypto (`COIN`), lalu top 50 berdasarkan volume, volatilitas, spread, dan blacklist |
| Strategi | TPLR: macro trend, EMA value zone, pullback, rejection candle, RSI, volume, dan ATR |
| Multi-timeframe | 1H untuk arah makro dan 15m untuk setup entry |
| BTC dominance filter | BTCDOMUSDT 1H untuk proyeksi entry altcoin dan 15m untuk exit reversal |
| Dynamic limit entry | Harga limit dihitung dari EMA 21 dan ATR pullback |
| Tiered timeout | GTD exchange-side 10/15 menit, plus dead-man switch 120 detik |
| Position sizing | Minimum dari batas exposure 25% saldo x leverage dan risk budget 1% wallet |
| Adaptive hard-stop | ATR/EMA55/swing 15m, jarak 1–5%, wajib aktif di exchange |
| Trailing stop | Price checkpoint, time-progressive lock, dan delayed breakeven |
| Reversal guard | Market close ketika struktur EMA 21/55 berbalik |
| Cooldown | Global, per-symbol, dan penalti consecutive loss |
| Recovery | Startup reconciliation terhadap posisi, entry order, dan algo stop Binance |
| User stream | WebSocket order dan position events, dengan REST sebagai sumber rekonsiliasi |
| Persistence | Atomic JSON state untuk posisi, order, stop, koneksi, statistik, dan cooldown |
| Dashboard | Authenticated command center, telemetry, performance, dan strategy admin |

## Persyaratan dan instalasi

- Python 3.10 atau lebih baru
- Akun Binance yang dapat menggunakan USDT-M Futures
- API key dengan izin futures trading

```bash
git clone <repository-url>
cd botfuture
python -m venv .venv
```

Aktifkan virtual environment dan install dependensi:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

```bash
# Linux/macOS
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Isi `.env`:

```env
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# testnet atau live
TRADING_MODE=testnet

DASHBOARD_PORT=5000
```

File `.env` sudah diabaikan oleh Git. Gunakan API key tanpa izin withdrawal dan aktifkan IP whitelist.

## Menjalankan aplikasi

```bash
# Bot saja
python run.py

# Bot dan dashboard dalam satu proses
python run.py --dashboard

# Dashboard saja
python run.py --dash-only
```

Dashboard tersedia di `http://localhost:5000` secara default. Dashboard memperbarui data setiap empat detik dan menampilkan health status, performance metrics, equity curve, active risk, posisi, stop protection, signal, serta 20 trade terakhir.

Login default adalah username `qais` dan password `User\@mis1`. Nilai ini dapat dioverride melalui `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`, dan `DASHBOARD_SECRET_KEY` di `.env`. Ganti secret key dengan string acak yang panjang pada VPS.

Halaman `/admin` hanya mengekspos parameter strategi yang di-whitelist. Perubahan divalidasi, langsung diterapkan ke proses bot, lalu disimpan atomik ke `strategy_overrides.json` agar tetap aktif setelah restart. API key, API secret, trading mode, mandatory stop, dan maksimum posisi tidak dapat diubah dari panel.

## Alur utama bot

Setiap iterasi berjalan dengan prioritas berikut. Position dan pending order dapat hidup bersamaan sesaat ketika terjadi partial fill:

```text
Ada pending order? -> refresh dead-man, periksa fill/partial/cancel/timeout
Ada posisi aktif?  -> verifikasi posisi dan stop wajib, lalu trailing/reversal
Tidak ada keduanya -> periksa cooldown dan saldo, scan, analisis, lalu entry
```

Interval utama diatur oleh `MAIN_LOOP_INTERVAL`, dengan nilai default 10 detik.

### 1. Inisialisasi

Ketika bot mulai:

1. Membuat `bot.pid` untuk mencegah dua instance berjalan dari working directory yang sama.
2. Memvalidasi API key dan membuat koneksi Binance Futures.
3. Memuat market dan state dari `bot_state.json` jika tersedia.
4. Membuat scanner, signal engine, order manager, trailing manager, dan reversal guard.
5. Melakukan startup reconciliation; Binance menjadi sumber kebenaran untuk posisi, pending entry, dan algo stop.
6. Menolak mulai trading jika API reconciliation gagal atau ditemukan lebih dari satu posisi.
7. Memastikan posisi hasil recovery memiliki stop aktif; jika tidak, bot memasang adaptive hard-stop.
8. Membuka authenticated user-data WebSocket untuk event order dan posisi.

Reconciliation hanya mengenali order dari ID yang tersimpan atau prefix `bf_`. Bot tidak lagi menjalankan sweep massal terhadap order manual/aplikasi lain.

### 2. Market scanning

Scanner lebih dulu menerima hanya kontrak swap USDT aktif dengan metadata Binance `underlyingType=COIN`. Komoditas, saham, ETF, indeks, dan kontrak TradFi seperti XAG ditolak secara fail-closed sebelum ranking volume. Dari maksimal 300 market crypto teratas, bot menerapkan filter:

- Coin tidak terdapat pada blacklist.
- Perubahan harga absolut 24 jam minimal 1%.
- Spread maksimal 0,05%.
- Skor akhir dihitung dari volume, volatilitas, dan spread.

### 3. Analisis signal TPLR

Signal engine memakai dua timeframe:

- **1H:** menentukan bias makro dari EMA 21/55/200, slope EMA21, dan ADX minimal 25.
- **15m:** mencari pullback dan liquidity rejection untuk entry.

Setup LONG membutuhkan:

- Bias 1H bullish; regime neutral hanya dapat lolos dengan skor minimal 92.
- EMA 21 tidak berada signifikan di bawah EMA 55.
- Harga tidak lebih dari 1,5% di atas EMA 21.
- Salah satu dari tiga candle terakhir menguji area EMA 21.
- Lower wick minimal 25% atau bullish engulfing.
- Close kembali di sekitar/atas EMA 21.
- RSI berada pada rentang 40-68.
- Volume minimal 0,8x volume SMA 20.
- ADX minimal 25.

Setup SHORT memakai kondisi kebalikan arah, dengan RSI 32-60 serta upper-wick atau bearish-engulfing sebagai rejection.

Skor sekarang dibangun dari nol berdasarkan kualitas wick, engulfing, volume, RSI, ADX, trend 1H, BTC, dan BTC.D. Minimum entry adalah 85. Pemilihan kandidat memakai 90% skor signal dan 10% kualitas scanner, sehingga likuiditas tidak lagi diabaikan.

#### BTC dominance market regime

Bot memakai `BTCDOMUSDT` dari Binance sebagai proxy relative strength BTC terhadap altcoin besar. BTC.D bukan arah absolut harga BTC, sehingga filter directional ini hanya diterapkan pada altcoin:

| Regime BTC.D 1H | Proyeksi | Dampak entry altcoin |
|---|---|---|
| Bullish: close > EMA 21 > EMA 55 dan momentum naik | BTC menguat relatif, altcoin mendapat tekanan | Block ALT LONG, bonus +5 untuk ALT SHORT |
| Bearish: close < EMA 21 < EMA 55 dan momentum turun | Altcoin menguat relatif terhadap BTC | Block ALT SHORT, bonus +5 untuk ALT LONG |
| Neutral/tidak tersedia | Regime belum jelas | Tidak memblokir entry |

Analisis memakai candle 1H yang sudah close dan di-cache selama 60 detik. Filter tambahan memakai trend absolut BTCUSDT 1H: alt-long diblokir saat BTC bearish dan alt-short diblokir saat BTC bullish.

> Seluruh keputusan entry, macro, dan reversal memakai candle yang sudah close. Candle aktif hanya dipakai sebagai referensi harga order.

### 4. Dynamic limit entry

Untuk signal valid, engine menghitung suggested entry menggunakan EMA 21 dan `0.35 x ATR`:

- LONG: limit di bawah harga pasar.
- SHORT: limit di atas harga pasar.
- Jarak entry dibatasi sekitar 0,15%-1,0% dari harga saat signal dianalisis.

Jika suggested entry tidak tersedia, bot memakai tiered fixed offset:

| Tier | Score | Offset | Timeout |
|---|---:|---:|---:|
| High conviction | >= 80 | 0,10% | 10 menit |
| Normal conviction | < 80 | 0,35% | 15 menit |

Entry baru hanya dikirim jika skor akhir minimal 85.

### 5. Sizing dan placement

Sebelum entry, bot:

1. Memastikan state tidak memiliki posisi atau pending order.
2. Memeriksa posisi untuk symbol tersebut langsung di Binance.
3. Mengatur isolated margin dan leverage 2x.
4. Menghitung exposure cap dari 25% saldo tersedia x leverage.
5. Menghitung risk cap dari jarak adaptive stop dan risk budget wallet.
6. Menyesuaikan harga dan amount dengan precision serta minimum market.
7. Membuat `newClientOrderId` ber-prefix `bf_` agar outcome timeout dapat direkonsiliasi tanpa duplicate order.
8. Mengirim limit GTD dengan expiry exchange-side dan menyimpannya sebagai pending order.
9. Mengaktifkan `countdownCancelAll` 120 detik untuk simbol entry dan me-refresh-nya setiap 30 detik selama order pending.
10. Merevalidasi setup setiap 30 detik; pending limit dibatalkan jika signal tidak lagi valid.

### Contoh sizing modal $35

Rumus sizing saat ini:

```text
exposure cap = available balance x 25% x leverage
risk budget  = available balance x 1%
risk cap     = risk budget / stop distance
notional     = min(exposure cap, risk cap)
```

Dengan saldo tersedia $35 dan leverage 2x:

```text
exposure cap                  = $35 x 25% x 2 = $17,50
risk budget                   = $35 x 1%       = $0,35
notional jika stop berjarak 5% = $0,35 / 5%    = $7,00
initial margin                = $7,00 / 2       = sekitar $3,50
```

Pada contoh stop 5%, risk cap lebih kecil sehingga bot membuka exposure sekitar **$7**, bukan $17,50. Jika stop hanya 2%, risk cap dan exposure cap sama-sama $17,50. Konfigurasi 25% berfungsi sebagai batas maksimum exposure, bukan target size yang harus selalu dipakai.

Minimum order futures bukan angka universal $25. Nilainya berbeda per symbol dan dapat berubah. Bot membaca `limits.amount.min` dan `limits.cost.min` dari market CCXT sebelum mengirim order. Sebagai contoh, spesifikasi Binance menyebut BTCUSDT memiliki minimum notional $100; target $36,75 tidak cukup untuk BTCUSDT, tetapi dapat cukup untuk symbol dengan minimum notional lebih rendah. Nilai aktual exchange tetap menjadi sumber kebenaran.

### 6. Pending order dan fill

Bot memantau pending order sampai:

- Fully filled: pending order dipindahkan menjadi posisi aktif.
- Partial fill saat masih open: posisi parsial langsung dicatat dan remainder segera dibatalkan.
- Jika cancel remainder gagal karena jaringan, posisi dan pending remainder sama-sama disimpan; GTD/dead-man tetap membatasi exposure.
- Canceled atau expired dengan partial fill: jumlah final yang terisi dijadikan posisi aktif.
- Canceled atau expired tanpa fill: pending state dibersihkan.
- Timeout: order dibatalkan dan bot masuk cooldown singkat.
- Timeout create-order/API tidak diulang secara buta; status dicari dengan `clientOrderId`.

Setelah posisi terdeteksi, dead-man entry dinonaktifkan dan bot wajib memasang/menemukan adaptive stop-market sebelum menjalankan trailing atau reversal logic.

### 7. Monitoring posisi

Untuk posisi aktif, bot setiap loop:

- Mengambil harga terbaru.
- Menghitung price profit, unrealized PnL, dan estimasi ROE.
- Memeriksa posisi aktual di Binance dengan status terpisah `found`, `empty`, atau `API_ERROR`.
- Pada `API_ERROR`, state posisi dipertahankan dan cycle dipause; error tidak dianggap posisi kosong.
- Menyamakan amount lokal jika ukuran posisi exchange berubah.
- Memverifikasi stop masih aktif dan ukurannya sama dengan posisi.
- Memperbarui trailing stop.
- Memeriksa reversal setiap 30 detik.
- Mendeteksi jika stop di exchange telah menutup posisi.

## Adaptive hard-stop, risk sizing, dan trailing ratchet

Initial hard-stop dihitung dari candle 15m yang sudah closed:

- LONG: sisi bawah EMA55 dan swing-low 20 candle, ditambah buffer ATR.
- SHORT: sisi atas EMA55 dan swing-high 20 candle, ditambah buffer ATR.
- Jarak stop dijepit pada minimum 1% dan maksimum 5% dari entry.
- Jika data indikator gagal diperoleh, fallback tetap berada pada batas maksimum 5%, bukan kembali ke -25%.

Ukuran order memakai dua batas sekaligus. Exposure tidak boleh melebihi `BALANCE_USAGE × LEVERAGE`, dan estimasi rugi pada initial stop tidak boleh melebihi `RISK_PER_TRADE_PERCENT` dari available wallet. Default risk budget adalah 1%. Akibatnya, size aktual dapat lebih kecil dari 25% margin ketika stop pasar cukup lebar.

Stop memakai Binance Algo Order reduce-only sehingga tetap tersedia ketika bot, laptop, atau koneksi offline. ACK stop diberi verification grace 8 detik untuk menghindari duplikasi akibat eventual consistency. Setelah grace, bot mengadopsi stop aktif dan menghapus duplicate STOP Algo Order pada simbol/sisi posisi yang sama.

Profit-taking memakai initial risk (`R`) dari jarak entry ke hard-stop:

- TP1: saat mencapai `min(1R, +1%)`, close 50% ukuran awal dan naikkan hard-stop sisa posisi ke +0,22%.
- TP2: saat mencapai `min(2R, +2%)`, close lagi 25% ukuran awal.
- Early BEP: pada profit harga +0,8%, hard-stop minimal dinaikkan ke +0,22%.
- Time stop: setelah 90 menit, posisi ditutup jika peak profit belum mencapai 0,5R.
- Semua partial close memakai reduce-only; stop sisa posisi dibuat sebelum stop lama dibatalkan.

Price-based trailing default:

| Profit harga | Stop mengunci profit |
|---:|---:|
| +2,0% | +0,3% |
| +3,5% | +1,5% |
| +5,0% | +3,0% |
| +6,5% | +4,5% |
| berikutnya | checkpoint - 2,0% |

Jika harga melewati beberapa checkpoint sekaligus, stop langsung dinaikkan ke checkpoint tertinggi. Stop baru dibuat sebelum stop lama dibatalkan sehingga posisi tidak melewati celah tanpa proteksi. Stop tidak pernah sengaja diturunkan.

Time-progressive lock:

| Umur posisi | Peak/current profit minimal | Stop minimal |
|---:|---:|---:|
| 90 menit | +1,4% | +0,8% |
| 120 menit | +1,7% | +1,2% |
| 150 menit | +2,0% | +1,6% |
| 180 menit | +2,2% | +1,9% |

Delayed breakeven aktif setelah 120 menit jika posisi pernah atau sedang mencapai +0,8%. Stop dikunci pada +0,22%.

Stagnation force-close tersedia di kode tetapi default-nya nonaktif. Posisi dibiarkan berjalan dan dikawal oleh stop berbasis harga serta waktu.

## Reversal guard

Reversal guard mengevaluasi timeframe 15m setiap 30 detik.

Reversal memakai dua tahap berdasarkan candle 15m yang sudah close:

- Close menembus EMA21: kurangi 50% posisi sekali dan resize hard-stop.
- Close menembus EMA55 atau EMA21/55 cross: tutup seluruh sisa posisi.

SHORT memakai kondisi kebalikan. Ketika reversal terkonfirmasi, bot mengirim reduce-only market order sementara hard-stop tetap aktif. Stop baru dibersihkan setelah market-close mendapat ACK, sehingga tidak ada celah posisi tanpa proteksi.

Untuk posisi altcoin, ada exit plan tambahan menggunakan dua candle BTCDOM 15m yang sudah close:

- ALT LONG dikurangi 50% jika dua candle mengonfirmasi struktur bullish BTC.D (`close > EMA 21 > EMA 55`) dan momentum naik.
- ALT SHORT dikurangi 50% jika dua candle mengonfirmasi struktur bearish BTC.D (`close < EMA 21 < EMA 55`) dan momentum turun.
- Reversal harga symbol tetap memiliki prioritas lebih tinggi.
- Posisi BTC tidak ditutup hanya karena perubahan BTC.D.
- Kegagalan mengambil data BTC.D tidak memicu close.

## Realized PnL dan cooldown

Setelah posisi ditutup, bot mencoba mengambil realized PnL dan komisi dari trade history Binance. Jika data tidak tersedia, bot menghitung fallback PnL dari harga entry/close dan estimasi round-trip fee.

| Kondisi | Durasi cooldown |
|---|---:|
| Setelah trade selesai | 20 menit global |
| Symbol yang baru ditutup | 60 menit |
| Pending order timeout/cancel | 1 menit global |
| Satu atau dua loss beruntun | 2x cooldown dasar |
| Tiga loss beruntun | Pause 30 menit, lalu counter di-reset |

Circuit breaker tambahan menghentikan entry baru selama 360 menit jika loss harian mencapai -2R atau profit factor enam hingga sepuluh trade terakhir turun di bawah 0,8. Win kecil di bawah +0,25R tidak mereset loss streak.

## State persistence

`bot_state.json` menyimpan:

- Status bot
- Posisi aktif
- Pending order
- Trailing stop dan checkpoint
- Signal terakhir
- Trade history dan statistik PnL
- Consecutive losses
- Global dan per-symbol cooldown
- Status stop protection dan kesehatan koneksi
- Hasil startup reconciliation dan event WebSocket terakhir

State ditulis melalui temporary file lalu `os.replace`, sehingga dashboard/proses lain tidak melihat JSON setengah tertulis. Saat startup, state lokal direkonsiliasi dengan posisi, regular order, dan algo order aktual di Binance sebelum scanning diizinkan.

## Konfigurasi penting

Semua parameter strategi dan operasional berada di `config.py`.

| Konfigurasi | Default | Keterangan |
|---|---:|---|
| `LEVERAGE` | 2 | Leverage futures |
| `MARGIN_MODE` | isolated | Mode margin |
| `BALANCE_USAGE` | 0.25 | Bagian saldo tersedia untuk sizing |
| `RISK_PER_TRADE_PERCENT` | 1.0 | Maksimum estimasi rugi initial stop terhadap wallet |
| `INITIAL_STOP_ATR_MULTIPLIER` | 1.5 | Jarak ATR dasar hard-stop |
| `INITIAL_STOP_ATR_BUFFER` | 0.25 | Buffer di luar struktur EMA/swing |
| `INITIAL_STOP_SWING_LOOKBACK` | 20 | Lookback swing candle closed |
| `INITIAL_STOP_MIN_DISTANCE_PERCENT` | 1.0 | Jarak minimum hard-stop dari entry |
| `INITIAL_STOP_MAX_DISTANCE_PERCENT` | 5.0 | Jarak maksimum hard-stop dari entry |
| `STOP_VERIFICATION_GRACE_SECONDS` | 8 | Masa tunggu visibilitas Algo Order setelah ACK |
| `STOP_DEDUPLICATION_ENABLED` | true | Sisakan satu stop per simbol/sisi posisi |
| `ENTRY_GTD_ENABLED` | true | Expiry entry dijalankan oleh Binance |
| `ENTRY_DEADMAN_ENABLED` | true | Kill switch pending entry per simbol |
| `ENTRY_DEADMAN_COUNTDOWN_MS` | 120000 | Countdown jika heartbeat bot berhenti |
| `MANDATORY_STOP_PROTECTION` | true | Posisi wajib memiliki stop aktif |
| `FAIL_CLOSE_IF_STOP_UNPROTECTED` | true | Market-close jika stop tidak dapat dipasang |
| `MIN_WALLET_BALANCE_USDT` | 35 | Saldo minimum untuk scan/entry |
| `SIGNAL_MIN_SCORE` | 85 | Minimum score global |
| `NEUTRAL_REGIME_MIN_SCORE` | 92 | Minimum score saat trend 1H neutral |
| `TRADING_TIMEFRAME` | 15m | Timeframe setup |
| `HIGHER_TIMEFRAME` | 1h | Timeframe trend makro |
| `BTCDOM_TIMEFRAME` | 1h | Regime BTC.D untuk entry altcoin |
| `BTCDOM_EXIT_TIMEFRAME` | 15m | BTC.D untuk exit reversal |
| `BTCDOM_STRICT_ENTRY_FILTER` | true | Block entry alt yang melawan BTC.D |
| `BTCDOM_EXIT_CONFIRMATION_CANDLES` | 2 | Closed candle untuk konfirmasi exit |
| `SCANNER_TOP_N` | 300 | Jumlah market berdasarkan volume |
| `CRYPTO_ONLY_SCANNER` | true | Hanya underlying type `COIN` |
| `MIN_24H_CHANGE_PERCENT` | 1.0 | Volatilitas minimum |
| `MAX_SPREAD_PERCENT` | 0.05 | Spread maksimum |
| `MAIN_LOOP_INTERVAL` | 10 | Interval loop dalam detik |
| `CANDLE_FETCH_LIMIT` | 250 | Jumlah candle analisis |
| `USER_STREAM_ENABLED` | true | WebSocket order dan posisi |

## Struktur proyek

```text
botfuture/
├── .env.example          # Template environment
├── requirements.txt      # Dependensi Python
├── README.md             # Dokumentasi proyek
├── run.py                # Entry point CLI
├── config.py             # Seluruh konfigurasi
├── bot.py                # Main loop dan orkestrasi
├── scanner.py            # Seleksi market
├── signal_engine.py      # Analisis TPLR dan reversal
├── order_manager.py      # Entry, cancel, close, stop, dan PnL
├── trailing_manager.py   # Price/time-based stop ratchet
├── reversal_guard.py     # Penutupan saat reversal
├── state_manager.py      # Persistence dan cooldown
├── user_stream.py        # Authenticated order/position WebSocket
├── logger_setup.py       # Console serta file logging
├── dashboard.py          # Flask monitoring dashboard
└── tests/                # Pengujian offline hardening lifecycle
```

Runtime files yang dibuat otomatis dan tidak di-commit:

```text
bot.pid
bot_state.json
strategy_overrides.json
logs/bot_YYYY-MM-DD.log
logs/trades_YYYY-MM-DD.log
```

## Batasan implementasi saat ini

Sebelum mode live, perhatikan hal berikut:

1. Automated test menggunakan fake exchange; belum ada integration test Binance Futures Testnet atau replay/backtest bawaan.
2. Dashboard memakai session login dan rate-limit sederhana, tetapi default bind tetap ke seluruh interface. Gunakan firewall/VPN/reverse proxy HTTPS pada VPS publik.
3. Dependensi belum seluruhnya dipin ke versi exact sehingga perubahan CCXT/Binance dapat memengaruhi perilaku API.
4. `countdownCancelAll` berlaku ke seluruh regular open order pada simbol yang sama. Gunakan akun/sub-account khusus dan jangan menaruh order manual pada simbol yang sedang dipakai bot.
5. Risk cap adalah estimasi berdasarkan harga stop; slippage, gap, fee, dan masalah likuiditas dapat membuat kerugian aktual lebih besar.
6. WebSocket mempercepat deteksi, tetapi REST reconciliation tetap diperlukan dan bot akan pause ketika status kritis tidak dapat dipastikan.

## Checklist sebelum mode live

- Jalankan cukup lama di Binance Futures Testnet.
- Gunakan akun atau sub-account khusus bot.
- Pastikan tidak ada posisi atau order manual pada akun tersebut.
- Batasi permission API hanya untuk futures trading, tanpa withdrawal.
- Aktifkan IP whitelist.
- Masukkan IP publik/egress jaringan atau VPN, bukan IPv4 lokal seperti `10.x.x.x`, `172.16-31.x.x`, atau `192.168.x.x`.
- Verifikasi precision, minimum notional, dan stop order untuk beberapa symbol.
- Uji restart ketika ada pending order dan ketika ada posisi aktif.
- Uji full fill, partial fill, timeout, network error, stop trigger, dan reversal close.
- Pantau log serta posisi Binance secara langsung selama tahap validasi.

## Tindak lanjut audit: exit, ledger, konfigurasi

- Partial-close menyimpan `exit_intent` sebelum submit dan memulihkannya melalui
  client order ID. Jangan menghapus state saat intent belum terselesaikan.
- PnL trade baru berstatus `estimated` sampai lifecycle fills, komisi dan funding
  direkonsiliasi ketika bot flat. `estimated_fx` berarti kurs biaya non-USDT
  memakai estimasi historical 1m close. Dashboard menampilkan status ini.
- Perubahan admin berlaku mulai siklus bot berikutnya dengan snapshot konsisten,
  termasuk dashboard proses terpisah yang memakai file override sama. Jalankan
  hanya satu proses bot dan satu admin writer.
- `TP_PRICE_CAP_ENABLED` tetap ON (perilaku lama). OFF memakai pure-R; belum
  terbukti lebih baik tanpa pengujian historis.
- Evaluasi offline: lihat [EXIT_EVALUATION.md](EXIT_EVALUATION.md).
  Status implementasi dan batasan: [STRATEGY_AUDIT.md](STRATEGY_AUDIT.md).

## Disclaimer

Trading futures memiliki risiko kehilangan modal yang tinggi. Leverage memperbesar keuntungan dan kerugian. Kode ini tidak menjamin profit dan bukan nasihat keuangan. Gunakan hanya modal yang siap ditanggung risikonya dan lakukan pengujian menyeluruh di testnet sebelum mempertimbangkan mode live.
