# Audit kode 27 September 2026

## Pembaruan entry 28 September 2026

Lima trade terbaru dalam state setelah perubahan sebelumnya menghasilkan satu
menang dan empat kalah. Sampel ini terlalu kecil untuk mengestimasi win rate
atau memilih parameter optimal. Kelimanya terjadi ketika filter harga BTC 1h
berstatus `neutral` (ADX sekitar 13–16); pemenang ASTER juga terjadi dalam
rezim itu, sehingga aturan baru *tidak* dijamin akan memperbaiki PnL.

Kelemahan entry yang ditemukan:

- Skor pola 15m sebelumnya dapat tinggi karena wick/engulfing/volume, walau
  penutupan candle kurang tegas. Wick saja tidak membuktikan buyer/seller
  mempertahankan level.
- Macro 1h dan BTC netral masih membolehkan alt entry. Struktur koin 4h tidak
  diuji sebelum order, sehingga setup 15m bisa hanya pantulan melawan tren.
- Scanner memberi bonus pada besarnya perubahan 24h, yang bisa menaikkan koin
  sudah jauh bergerak; tidak ada batas nominal quote volume dan bid/ask kosong
  dapat lolos.

Perubahan default bersifat konservatif dan dapat dikendalikan dari admin:

1. Alt entry hanya saat rezim BTC 1h searah; saat BTC netral, tunggu. Sakelar
   `ENTRY_SKIP_NEUTRAL_BTC` dapat digunakan untuk membandingkan hasil, tetapi
   jangan mematikan proteksi hanya karena frekuensi trade turun.
2. Tren 4h koin harus searah dengan sinyal 15m/1h, memakai candle 4h yang telah
   tutup. Data 4h kurang dari 202 candle atau indikator tidak tersedia => tunggu.
3. Candle penolakan 15m harus tutup searah posisi, melampaui/menyamai penutupan
   sebelumnya, dekat EMA21 (maksimal 1 ATR), pada 35% teratas untuk long atau
   terbawah untuk short, dengan volume minimal 1x SMA20.
   Harga live juga tidak boleh lebih dari 1.5 ATR dari EMA21 ke arah entry,
   agar bot tidak mengejar pergerakan setelah candle sinyal tutup.
4. Scanner mensyaratkan quote volume 24h >= 10 juta USDT dan bid/ask valid;
   ranking hanya memakai likuiditas serta spread, bukan besar perubahan 24h.
5. Semua filter baru ikut revalidasi pending limit tiap 30 detik. Tidak ada
   perubahan TP/SL, leverage, alokasi modal, maupun mode live/testnet.

Hipotesis ini perlu diuji melalui replay candle historis yang tidak melihat
candle masa depan, lalu paper/shadow trading dengan biaya, spread, slippage,
funding, fill rate limit order, dan hasil berdasarkan rezim. Pantau *jumlah*
trade, expectancy bersih dalam R, MFE/MAE, profit factor, dan alasan penolakan;
win rate saja tidak cukup. Jangan mengubah ambang berdasarkan lima trade ini.

## Alur yang ditinjau

Scanner crypto USDT perpetual -> filter volume/volatilitas/spread -> TPLR
15m dengan macro 1h, BTC dan BTCDOM -> ranking -> limit entry -> penanganan
fill -> exchange hard-stop -> partial TP/BEP/trailing/reversal/time-stop ->
rekonsiliasi dan cooldown/circuit breaker. WebSocket memberi petunjuk event;
REST tetap dipakai untuk konfirmasi posisi.

## Perbaikan dalam audit ini

- Entry diblokir jika sudah ada posisi atau pending entry. Kandidat harus
  memenuhi skor minimum sebelum ranking; pemenang dianalisis ulang setelah scan.
- ACK market-close bukan bukti posisi kosong. Stop/state dipertahankan sampai
  query posisi mengonfirmasi kosong, termasuk ketika close ditolak reduce-only.
- Network timeout close tidak langsung diulang dalam pemanggilan yang sama.
  Siklus monitoring berikutnya tetap melakukan rekonsiliasi REST.
- Penggantian stop tidak membatalkan ID yang baru saja diadopsi kembali.
- Pemulihan/resize stop mempertahankan harga proteksi terbaik dan initial R.
- Partial TP yang gagal/resize tertunda mengakhiri siklus sebelum exit lain.
- PnL dengan close-order ID eksplisit tidak mengambil fill order lama ketika
  fill yang dimaksud belum muncul; masih memakai estimasi bila belum tersedia.
- Admin menolak NaN/infinity, integer pecahan, TP2 <= TP1, dan lock BEP >= trigger.

## Tindak lanjut yang diterapkan

- **Partial exit recovery:** `exit_intent` berisi client ID unik disimpan sebelum
  submit. Setelah timeout/restart, query ID yang sama dan catat actual filled
  quantity. Akuntansi partial + penghapusan intent disimpan dalam satu replace
  atomic. Jika hasil tidak diketahui, jangan submit lagi atau menghapus stop.
- **Lifecycle ledger:** trade baru ditandai `estimated`, lalu ketika bot flat,
  rekonsiliasi satu trade per menit (minimal 120 detik setelah close, retry per
  trade 5 menit). Entry/exit fills dipaginasi dengan fromId, funding dipaginasi
  per page/time window, ID dideduplikasi. Net PnL menggantikan estimasi lifecycle
  dan memperbarui total/R/loss streak. Riwayat legacy tidak otomatis ditulis ulang.
- **Komisi non-USDT:** konversi memakai historical 1m close dan ditandai
  `estimated_fx`, bukan angka eksak. Missing/truncated/overlapping history tetap
  estimasi dengan `ledger_error`; tidak dianggap biaya nol. Gunakan akun khusus
  bot untuk atribusi funding/posisi per simbol.
- **Konfigurasi:** seluruh modul strategi membaca snapshot yang sama per siklus.
  File override dibaca pada awal siklus, termasuk bila dashboard proses terpisah.
  Save admin dan snapshot diserialisasi dalam proses; gunakan satu admin writer.
  Default path override sekarang relatif ke direktori project, bukan cwd.
- **TP/evaluasi:** admin memiliki `TP_PRICE_CAP_ENABLED` (default tetap ON).
  OFF berarti pure-R; label exit membedakan capped dan R. Evaluator offline
  tersedia dengan fee/slippage/funding, walk-forward, purging, dan kelompok
  long/short/regime. Lihat `EXIT_EVALUATION.md` untuk asumsi dan format dataset.

Query client order ID dan histori transaksi mengikuti dokumentasi resmi
[Binance Trade REST](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade).
Funding menggunakan [Binance Account REST](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account).

## Batasan yang tetap ada

- Exit intent yang tidak ditemukan oleh exchange tetap diblokir; perlu pemeriksaan
  client ID, posisi dan order aktual. **Jangan mengosongkan bot_state.json** untuk
  melewati blokir. Startup dapat berhenti fail-closed sampai intent terselesaikan;
  hard-stop exchange yang sudah ada tidak dihapus.
- Persisted intent baru ini khusus partial exit, bukan jaminan exactly-once semua
  order. Full close tetap memakai REST confirmation dan pengamanan sebelumnya.
- Ledger adalah snapshot API setelah settlement delay, bukan laporan audit final
  Binance. Biaya yang muncul lebih lambat dan trade legacy perlu pemeriksaan.
  Circuit breaker tetap memakai estimasi sampai rekonsiliasi tersedia; dashboard
  menampilkan kualitas PnL.
- Evaluator fixed-entry bukan replay penuh strategi/live execution. Belum ada
  hasil out-of-sample nyata; parameter live tidak dioptimasi dari data sintetis.

## Temuan awal (konteks sebelum tindak lanjut)

1. TP saat ini `min(R * multiplier, price_cap)`. Dengan cap 1%/2%, TP bukan
   selalu 1R/2R. Jangan menilai hasil berdasarkan label alasan exit saja.
   Bandingkan cap vs pure-R melalui replay dengan fee, funding dan slippage
   sebelum mengganti parameter live. Audit ini tidak mengganti default TP.
2. Statistik PnL belum ledger lengkap: biaya entry, funding, komisi non-USDT,
   pagination fills, dan partial close ambigu perlu rekonsiliasi tersendiri.
   Profit factor/circuit breaker yang bergantung pada angka ini punya batasan.
3. Partial exit masih perlu persisted client-order intent dan recovery setelah
   timeout/restart. Perbaikan close di atas bukan jaminan exactly-once execution.
4. Evaluasi walk-forward/out-of-sample per market regime dan arah long/short
   diperlukan. Unit test bukan bukti profitability atau validasi Binance live.
5. Admin mengubah globals; snapshot konfigurasi konsisten per siklus dan
   sinkronisasi antarproses dashboard/bot masih perlu pekerjaan lanjutan.

Mode exchange, leverage, alokasi modal dan file state operasional tidak diubah
oleh audit ini. Tidak ada bot live dijalankan ulang maupun order dikirim saat uji.
