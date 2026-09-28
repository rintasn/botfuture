# Evaluasi exit offline

`evaluate_exits.py` tidak terhubung ke Binance, tidak membaca API key, dan tidak
mengubah konfigurasi/state bot. Ini pembanding **fixed-entry exit**, bukan
backtest lengkap TPLR/scanner/limit-fill/reversal guard.

## Menjalankan di CMD

```bat
.venv\Scripts\activate.bat
python evaluate_exits.py episodes.json --train 30 --test 10 --fee-bps 5 --slippage-bps 3
```

`episodes.json` harus berisi entry serta lintasan OHLC **historis nyata**.
Contoh format satu episode (angka ilustrasi, bukan data hasil trading):

```json
[
  {
    "symbol": "BTC/USDT:USDT",
    "side": "long",
    "regime": "bullish",
    "entry_time": 1700000000000,
    "entry_price": 100,
    "amount": 1,
    "risk_pct": 2,
    "candles": [
      {"time": 1700000900000, "open": 100, "high": 101, "low": 99.5, "close": 100.8, "funding_rate": 0},
      {"time": 1700001800000, "open": 100.8, "high": 102, "low": 100.6, "close": 101.8, "funding_rate": 0}
    ]
  }
]
```

- Epoch timestamp dalam millisecond; bar pertama harus setelah entry. Jangan
  menyertakan OHLC sebelum fill sebagai bar setelah entry. `time` mewakili akhir bar.
- `regime` harus diketahui pada waktu entry, bukan dibuat dari hasil sesudahnya.
- `funding_rate` hanya diisi pada bar yang memuat event funding (misalnya 0.0001),
  bukan disalin ke setiap bar. Nol berarti diasumsikan tidak ada funding di bar itu.
- Fee/slippage dalam basis points; nilai bawaan hanyalah asumsi simulasi.
- Kumpulkan minimal `train + 1` episode non-overlap agar ada hasil out-of-sample.

## Model dan batasan

Membandingkan cap 1%/2% dengan pure 1R/2R, partial close 50%/25%, early BEP
0.8% -> 0.22%, stepped trailing (2%, step 1.5%, offset 2%), time-stop 90 menit
bila MFE close < 0.5R. Nilai ini eksplisit sebagai baseline; **bukan membaca
setting admin yang sedang live**. Exit tambahan/reversal dan precision/minimum
notional exchange belum dimodelkan.

Hard-stop diperiksa sebelum keputusan close-based pada tiap bar; gap dieksekusi
di open yang lebih buruk. Profit/BEP/trailing diputuskan pada close dan stop baru
berlaku bar berikutnya. Jalur tick, latensi, spread dinamis, dan market impact
tidak diketahui dari OHLC. Sisa posisi ditutup pada akhir lintasan yang diberikan.

Walk-forward memilih kebijakan dari mean-R di training saja. Trade training yang
belum berakhir sebelum entry test pertama dibuang (purging). Output membedakan
perbandingan seluruh dataset dari hasil out-of-sample, termasuk arah/regime.

Belum ada dataset historis lintasan lengkap di audit ini, sehingga belum ada
bukti bahwa pure-R lebih baik. Jangan mengganti live hanya karena unit test lulus.
