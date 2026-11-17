# AEGIS-1 — Adaptive Execution & Guarded Investment System

Sistem trading modular: Python untuk data, indikator, sinyal, risiko, eksekusi,
dan audit; satu file HTML untuk console pemantauan real-time.

Paket ini berjalan sebagai **mesin eksekusi paper** (simulasi) dengan model biaya
eksplisit — fee maker/taker, slippage, spread. Seluruh guardrail risiko, audit
log, dan dashboard berfungsi penuh. **Tidak ada kode di paket ini yang mengirim
order nyata ke exchange.** Alasannya, dan apa yang harus dibangun untuk live,
ada di bagian [Batas paket](#batas-paket-dan-langkah-menuju-live).

> Isi berkas ini bukan nasihat investasi. Tidak ada janji profit di mana pun.
> Angka pada dashboard adalah hasil simulasi.

**Situs (GitHub Pages):** <https://murohito9-sudo.github.io/aegis-one/>

| Halaman | Isi |
| --- | --- |
| [index](https://murohito9-sudo.github.io/aegis-one/) | pemilih mode: demo atau live |
| [demo.html](https://murohito9-sudo.github.io/aegis-one/demo.html) | console dengan data contoh tertanam + validator strategi JSON |
| [live.html](https://murohito9-sudo.github.io/aegis-one/live.html) | langkah menjalankan bot sungguhan (lokal/VPS) |

Mode demo tidak memakai backend dan tidak mengirim order, tetapi **memakai data
pasar sungguhan**: halaman menarik candle publik dari Binance (dengan cadangan
Bybit, OKX, Kraken, Coinbase), menghitung EMA/RSI/MACD/ATR sendiri di browser,
menilai sinyal konfluensi, lalu menjalankan eksekusi paper dengan fee dan
slippage. Ledger posisi, trade, dan ekuitas disimpan di `localStorage` browser
itu saja sehingga kunjungan berikutnya melanjutkan sesi yang sama; tombol
**reset** menghapusnya.

Saat pertama dibuka, mesin menjalankan *pemanasan* atas ±700 candle terakhir
supaya ledger langsung berisi jejak keputusan, bukan menunggu 15 menit per bar.
Setelah itu ia hanya mengevaluasi bar yang sudah tutup — sama seperti bot
sungguhan. Kalau semua sumber data gagal (CORS, blokir wilayah, atau offline),
bilah status berubah merah dan menyebut penyebabnya; tidak ada angka yang
dikarang untuk menutupi kegagalan.

**Strategi JSON.** Halaman demo menerima berkas strategi dari pengguna
(parameter indikator, filter, bobot konfluensi, setelan risiko) dan
memeriksanya di browser: rentang nilai, aturan silang seperti `ema_fast <
ema_slow`, total bobot = 1, dan kunci tak dikenal. Hasilnya juga berupa potongan
`.env` siap tempel untuk parameter yang punya variabel lingkungan
(`AEGIS_EMA_FAST`, `AEGIS_MIN_CONFLUENCE`, `AEGIS_RISK_PER_TRADE`, dst).

Setelah lolos validasi, strategi itu masuk pustaka dan bisa dipakai tiga cara
di halaman demo: **dipasang sebagai strategi aktif** (ukuran posisi, aksi sinyal
di tabel riwayat, dan chip `STRATEGI` di header console ikut berubah),
**dibandingkan berdampingan** dengan strategi lain (jumlah sinyal lolos, yang
ditolak skor atau reward:risk, ukuran posisi risk-based vs setelah batas
exposure, plus tabel parameter berbeda), dan **diekspor** sebagai ringkasan
Markdown. Tersedia tiga preset bawaan sebagai pembanding.

Halaman statis hanya memvalidasi, menghitung ulang, dan menampilkan — bot tidak
berubah karena unggahan itu; parameter tanpa env var harus diubah di
`config.py`.

---

## 1. Struktur folder

```
aegis1/
├── config.py            # parameter strategi, risiko, eksekusi (env: prefix AEGIS_)
├── data.py              # feed OHLCV (ccxt publik / CSV / sintetis) + gate kualitas data
├── indicators.py        # EMA, RSI Wilder, MACD, ATR, ROC, relative volume, swing, S/R, BOS
├── strategy.py          # regime detection, skor konfluensi, sinyal + alasan & veto
├── risk.py              # position sizing, SL/TP, trailing, partial TP, batas harian, drawdown
├── execution.py         # PaperBroker (fee, slippage, timeout, dedupe) + adapter live yang menolak
├── analytics.py         # metrik kinerja, daftar P&L per trade, ekspor CSV/MD
├── brain.py             # AI Core: briefing & keputusan berbasis aturan (tanpa LLM)
├── microstructure.py    # order book, depth, imbalance, trade flow
├── news.py              # sentimen berita berbasis leksikon + provider RSS
├── onchain.py           # z-score metrik on-chain dari data yang kamu suplai
├── portfolio.py         # bobot, konsentrasi, DCA, saran rebalancing
├── evaluation.py        # kalibrasi, Brier, drift performa
├── monitor.py           # scheduler, health, heartbeat, backup/restore
├── security.py          # rantai hash audit, rate limit, pemindai rahasia
├── voice.py             # narasi briefing + TTS opsional
├── intelligence.py      # MTF, divergensi RSI, pola candle, squeeze (tanpa look-ahead)
├── live.py              # adapter eksekusi nyata Binance (default mati, pagar berlapis)
├── logger.py            # audit log SQLite + JSONL, alert lokal
├── dashboard.py         # backend FastAPI: state, history, aksi dua langkah, WebSocket
├── main.py              # loop utama, manajemen posisi, exception handling, graceful shutdown
├── tests.py             # 122 unit test (sizing, risiko, sinyal, gate, replay, analitik)
├── static/
│   └── index.html       # "AEGIS-1 Trading Console" — satu file, tanpa CDN
├── deploy/
│   └── aegis1.service   # unit systemd untuk 24/7 + restart otomatis
├── .github/workflows/ci.yml  # CI: test di Python 3.11 & 3.12 + smoke offline
├── Dockerfile           # image runtime, pengguna non-root, healthcheck
├── .dockerignore
├── data/                # SQLite, JSONL, kill-switch (dibuat saat runtime)
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

---

## 2. Instalasi

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # opsional; default sudah aman
python tests.py                                       # 122 test harus OK
```

Python 3.11+ (diuji pada 3.12). Tidak ada dependensi berbayar.

## 3. Menjalankan

```bash
# Demo tanpa jaringan (data sintetis deterministik, bagus untuk uji guardrail)
python main.py --offline --interval 0.5

# Data publik dari exchange lewat ccxt (read-only, tanpa API key)
python main.py

# Dari candle milik sendiri (kolom: ts,open,high,low,close,volume)
python main.py --csv data/BTCUSDT_15m.csv

# Tanpa dashboard, berhenti setelah N siklus (untuk CI)
python main.py --offline --ticks 50 --no-dashboard
```

Lalu buka **http://127.0.0.1:8787**. Dashboard hanya mengikat ke `127.0.0.1`
secara default; untuk akses jarak jauh pakai SSH tunnel
(`ssh -L 8787:127.0.0.1:8787 user@vps`), jangan membuka port ke publik tanpa
reverse proxy + autentikasi.

Nilai `[ISI: …]` pada brief belum diisi, jadi default yang dipakai: **crypto,
Binance, BTC/USDT, 15m, 10.000 USDT**. Semua bisa diubah di `.env` tanpa
menyentuh kode.

## 4. Konfigurasi

Semua parameter dibaca dari environment dengan prefix `AEGIS_`
(lihat `.env.example`). Yang paling sering diubah:

| Variabel | Default | Arti |
|---|---|---|
| `AEGIS_SYMBOL` | `BTC/USDT` | pair |
| `AEGIS_TIMEFRAME` | `15m` | timeframe candle |
| `AEGIS_EXCHANGE` | `binance` | id exchange ccxt (data publik) |
| `AEGIS_INITIAL_EQUITY` | `10000` | modal awal simulasi |
| `AEGIS_RISK_PER_TRADE` | `0.01` | 1% equity per trade |
| `AEGIS_MAX_TOTAL_EXPOSURE` | `0.20` | 20% equity |
| `AEGIS_MAX_DAILY_LOSS` | `0.03` | stop entry saat rugi harian 3% |
| `AEGIS_MAX_DRAWDOWN` | `0.10` | mode SAFE saat drawdown 10% |
| `AEGIS_MIN_CONFLUENCE` | `0.70` | ambang skor sinyal (0–1) |
| `AEGIS_OFFLINE` | `false` | `true` = data sintetis |

Kredensial: `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET`,
`EXCHANGE_API_PASSPHRASE`. Paket ini hanya membaca **status terisi/kosong**
(`config.credential_presence()`), tidak pernah nilainya. Tidak ada key di kode,
di `.env.example`, di database, di log, atau di browser.

---

## 5. Cara kerja strategi

Satu baris: **enam konfirmasi harus searah, kalau tidak, tidak ada trade.**

### 5.1 Trend filter
Harga di atas EMA50 **dan** EMA50 di atas EMA200 (atau breakout terkonfirmasi:
close menembus swing high terakhir dengan harga di atas EMA50). Kebalikannya
untuk short. Melawan tren utama diizinkan hanya bila ambang konfluensi dinaikkan
+0,10.

### 5.2 Momentum
RSI(14) metode Wilder: long butuh 45 ≤ RSI ≤ 78. Batas atas 78 penting — RSI 90
berarti harga sudah jauh dari basis, dan mengejar di situ adalah cara klasik
kalah. MACD(12,26,9) harus searah (line > signal dan histogram > 0 untuk long).
ROC(10) harus searah.

### 5.3 Volatilitas
ATR(14) Wilder dipakai tiga kali: menentukan jarak stop (1,8 ATR), menentukan
trailing (1,5 ATR), dan mengklasifikasi regime (persentil ATR ≥ 90 → volatilitas
tinggi, ambang konfluensi +0,10).

### 5.4 Struktur pasar
Swing high/low terkonfirmasi (fraktal 5 kiri–5 kanan). Bar terakhir **tidak
pernah** dianggap swing karena butuh konfirmasi di kanan — ini yang mencegah
look-ahead. Dari swing itu: level support/resistance terdekat, status break of
structure (BOS), dan jarak bar sejak BOS.

### 5.5 Volume
Volume relatif terhadap rata-rata 20 bar **sebelumnya** (bukan termasuk bar
berjalan). Minimum 1,0× untuk entry. Di bawah 0,55× regime dianggap likuiditas
rendah dan bot berhenti.

### 5.6 Regime
`trending_up`, `trending_down`, `ranging`, `high_volatility`, `low_liquidity`.
Aturannya: `low_liquidity` → tidak ada trade sama sekali; `ranging` → butuh BOS;
`high_volatility` → ambang konfluensi naik.

### 5.7 Skor konfluensi
Bobot: trend 0,25 · struktur 0,20 · momentum 0,15 · MACD 0,15 · volume 0,15 ·
ROC 0,10. Entry butuh skor ≥ 0,70 (0,80 di volatilitas tinggi atau saat melawan
tren) **dan** trend + MACD + volume tidak boleh gagal. Tidak ada sinyal tunggal
yang bisa memicu entry.

### 5.8 Stop, target, reward:risk
Stop = yang lebih jauh antara `entry − 1,8 ATR` dan `swing low − 0,25 ATR`
(short: kebalikannya). Kalau swing terlalu jauh (> 1,8× batas ATR), stop memakai
ATR dan itu dicatat di alasan sinyal. Target = resistance/support struktural bila
jaraknya ≥ 2R; kalau resistance lebih dekat dari 2R, setup **ditolak** — itu
overhead supply. Target dibatasi maksimum 4R.

Setiap sinyal menyimpan `reasons` (kenapa layak) dan `vetoes` (kenapa ditolak),
keduanya masuk audit log. Contoh veto yang akan Anda lihat di log:
`RSI 91.7 ekstrem/tidak mendukung arah LONG`, `support 45663.41 terlalu dekat (<2.0R)`,
`skor konfluensi 0.70 < ambang 0.80`.

---

## 6. Guardrail risiko

### 6.1 Batas keras
| Guardrail | Nilai default |
|---|---|
| Risiko per trade | 1% equity |
| Posisi terbuka maksimum | 3 |
| Total exposure maksimum | 20% equity |
| Rugi harian maksimum | 3% → mode SAFE, entry berhenti sampai hari berikutnya |
| Drawdown maksimum | 10% → mode SAFE, butuh konfirmasi manual |
| Leverage maksimum | 1× |
| Reward:risk minimum | 2,0 |
| Spread maksimum | 8 bps |
| Cooldown setelah loss | 3 bar |
| Order per bar | 1 (deduplikasi per `signal_id`) |

### 6.2 Position sizing
```
position_size = (equity × risk_percent) / |entry − stop|
```
lalu dibatasi exposure/leverage, dibatasi porsi slot posisi, dan **selalu
dibulatkan ke bawah** ke `step_size` (`round_down_to_step`, memakai `Decimal`,
tidak pernah membulatkan ke atas). Ditolak bila di bawah `min_qty` atau
`min_notional`.

> **Catatan jujur dari hasil uji.** Dengan stop 1,8 ATR (jarak 1–2%) dan exposure
> maksimum 20%, batas exposure-lah yang mengikat lebih dulu, bukan 1%. Artinya
> risiko efektif per trade mendarat di sekitar 0,2–0,3%, bukan 1%. Ini bukan bug:
> sistem mengambil batas yang paling konservatif. Bila Anda memang ingin 1%
> benar-benar terpakai, naikkan `AEGIS_MAX_TOTAL_EXPOSURE` atau perkecil
> pengali ATR — dan sadari bahwa keduanya menaikkan risiko.

### 6.3 Checklist pra-trade 16 poin
Order hanya dikirim bila **semua** lolos. Satu gagal = order tidak dikirim, dan
alasannya masuk log (`pretrade_blocked`).

1. sinyal valid · 2. stop terdefinisi · 3. reward:risk ≥ 2 · 4. ukuran posisi
valid · 5. batas rugi harian belum tercapai · 6. kill-switch/pause tidak aktif ·
7. spread & likuiditas wajar · 8. slot posisi tersedia · 9. total exposure
setelah order masih ≤ 20% · 10. drawdown < 10% · 11. gate kualitas data lolos ·
12. tidak ada order pending · 13. bar ini belum dipakai untuk order ·
14. cooldown pasca-loss terlewati · 15. tidak ada blokir event volatilitas ·
16. izin live sesuai konfigurasi.

### 6.4 Gate kualitas data (fail-closed)
Sebelas pemeriksaan: jumlah bar ≥ 260, tanpa sel kosong, high/low/close
konsisten, timestamp unik dan naik monoton, ≤ 2% bar bervolume nol, bar terakhir
tidak lebih tua dari 3× timeframe, tanpa lompatan harga > 25%, spread ≤ 8 bps,
kuotasi bid < ask. Gagal satu → tidak ada sinyal yang diproses sama sekali.

### 6.5 Manajemen posisi
- **SL dan TP dikirim ke broker segera setelah fill entry** (`STOP_MARKET` dan
  `TAKE_PROFIT_MARKET`), bukan hanya dipantau di memori.
- Partial take profit 50% pada 1R dan pada 2R; qty SL/TP ikut disesuaikan agar
  tidak melebihi sisa posisi.
- Trailing stop aktif setelah 1R, jarak 1,5 ATR, hanya bergerak ke arah profit.
- Jaring pengaman: bila harga sudah melewati level SL/TP tetapi order tidak ada
  atau tidak terisi, posisi ditutup eksplisit (`stop_loss_safety` /
  `take_profit_safety`).
- Exit karena trailing dicatat sebagai `trailing_stop`, bukan `stop_loss`, supaya
  statistik R tidak menyesatkan.

---

## 7. Eksekusi dan biaya (paper)

`PaperBroker` memodelkan hal-hal yang biasanya diabaikan backtest naif:

| Hal | Model |
|---|---|
| Fee maker | 0,02% |
| Fee taker | 0,05% (limit yang menyeberang book dihitung taker) |
| Slippage market/stop | 1,5 bps di atas bid/ask |
| Fill limit | hanya bila harga menyentuh limit |
| Stop/TP | terisi saat harga melewati level, lalu market + slippage |
| Timeout limit | 20 detik → `EXPIRED` |
| Order ganda | `signal_id` yang sama ditolak (`OrderRejected`) |
| Partial fill | didukung (status `PARTIALLY_FILLED`) |
| Rekonsiliasi | setiap order/fill ditulis ke SQLite + JSONL, termasuk `exchange_id`, harga eksekusi, fee, slippage, timestamp |

## 8. Audit dan rekonstruksi

Tujuh tabel: `signals`, `orders`, `fills`, `positions`, `equity`, `events`,
`actions`. Setiap siklus menulis snapshot ekuitas; setiap sinyal menulis alasan
dan veto; setiap order menulis state lengkap sebelum dan sesudah. Jejak yang sama
ditulis append-only ke `data/aegis1.jsonl`. Untuk merekonstruksi satu keputusan:
cari `signal_id` di `signals`, lihat `checks` di event `order_summary`, lalu
`orders` → `fills` → `positions`.

```bash
sqlite3 data/aegis1.sqlite3 "select ts,kind,message from events order by id desc limit 20;"
```

## 9. Dashboard

Satu file `static/index.html` (tanpa CDN, tanpa framework, tanpa
localStorage/sessionStorage). Isi: status koneksi, mode (PAPER/SAFE/PAUSED/
KILL-SWITCH), equity, available balance, unrealized & realized PnL, win rate,
profit factor, average R, max drawdown, tabel posisi terbuka (entry, size, SL, TP,
PnL, R, age, trailing), panel risiko (risk/trade, rugi harian, exposure,
drawdown, slot), chart candlestick (open/high/low/close) dengan EMA50/EMA200 dan marker entry/exit, riwayat
order/trade/sinyal, dan log eksekusi.

Halaman console dibagi tiga tab: **Ringkasan** (briefing, KPI, chart, panel risiko,
posisi terbuka), **Riwayat & Laporan** (order/trade/sinyal, log eksekusi, laporan
kinerja), dan **Strategi** (pemilih strategi aktif, perbandingan, validator JSON).
Chart digambar ulang saat tab Ringkasan dibuka kembali, karena canvas berlebar nol
saat panelnya tersembunyi.

Chart bisa di-zoom dan digeser: scroll di atas chart untuk memperbesar/memperkecil
(15 bar sampai seluruh data), tahan-dan-geser untuk pan ke candle lama, klik dua
kali atau tombol ⟲ untuk kembali. Saat tampilan berada di ujung kanan, chart
otomatis mengikuti candle baru; kalau kamu sedang melihat candle lama, tampilannya
tidak melompat. Indikator "102/200 bar" menunjukkan jendela yang sedang terlihat. Tautan dalam halaman (`#report`, `#banding`,
`#strategi`) otomatis membuka tab yang sesuai.

Tombol: **PAUSE TRADING**, **RESUME TRADING**, **CLOSE ALL POSITIONS**,
**EMERGENCY KILL-SWITCH**. Semuanya dua langkah: klik → ringkasan dampak →
ketik frasa konfirmasi (`PAUSE AEGIS-1`, `TUTUP SEMUA POSISI`,
`AKTIFKAN KILL SWITCH`, …). Token konfirmasi berlaku 120 detik, sekali pakai, dan
hangus bila frasa salah.

Endpoint: `GET /`, `GET /api/state`, `GET /api/history`, `GET /healthz`,
`GET /api/report`, `GET /api/report.csv`, `GET /api/report.md`,
`WS /ws?csrf=…`, `POST /api/actions/{pause|resume|close_all|kill}/request`,
`POST /api/actions/{action}/confirm`. Setiap request butuh header `X-AEGIS-CSRF`
dari `<meta name="aegis-csrf">`; WebSocket tanpa token ditolak (kode 4403).

Kill-switch melakukan tiga hal sekaligus: menghentikan entry, membatalkan semua
order pending, menutup semua posisi — lalu menulis file `data/KILL_SWITCH`.
Selama file itu ada, bot tidak kembali sendiri; hapus file dan restart secara
sadar. Resume saat mode KILL ditolak dengan HTTP 409.

## 9b. Laporan kinerja

Bagian **Laporan Kinerja** di dashboard (dan `analytics.py` di baliknya)
menjawab pertanyaan "berapa untung-rugi tiap order":

* kartu metrik: PnL bersih (setelah fee masuk), win rate, profit factor,
  payoff, expectancy per trade (uang dan R), Sharpe & Sortino per trade,
  max drawdown, recovery factor, total fee, rata-rata durasi trade;
* tabel trade tertutup: waktu, pair, arah, entry, exit, PnL, net, R, MAE, MFE,
  fee, alasan tutup;
* agregat per simbol, per arah, per jam tutup (UTC), dan per alasan tutup;
* tabel order: qty, harga, notional, status, fee, slippage bps;
* unduhan: `GET /api/report.csv` (tabel trade) dan `GET /api/report.md`
  (laporan Markdown lengkap dengan tabel).

Definisi angka tertulis di kepala `analytics.py`: `gross_pnl` sudah dipotong fee
keluar dan fee partial tetapi belum fee masuk; `net_pnl` memotong semuanya.
Sharpe/Sortino dihitung per trade dari return terhadap ekuitas awal dan
**tidak disetahunkan** — jumlah trade per tahun berbeda antar pasar, jadi angka
setahunannya akan menyesatkan. MAE/MFE dicatat selama posisi hidup, dalam R.

## 9c. Forex, replay historis, dan filter sesi

Pasar non-crypto tidak punya feed sendiri; jalurnya CSV (atau CSV + replay).

* **Metadata instrumen.** `AEGIS_TICK_SIZE`, `AEGIS_STEP_SIZE`,
  `AEGIS_MIN_QTY`, `AEGIS_MIN_NOTIONAL`, `AEGIS_PIP_SIZE`,
  `AEGIS_PRICE_DECIMALS`, `AEGIS_QUOTE_CURRENCY`. Default paket ini untuk
  crypto; untuk EURUSD setel tick `0.00001` dan step ke satuan unit/lot.
* **Spread dari data.** Kolom `bid`/`ask` atau `spread` di CSV dipakai apa
  adanya (`AEGIS_USE_DATA_SPREAD=true`). Kalau tidak ada, spread diambil dari
  `AEGIS_SPREAD_BPS` — bukan lagi dipalsukan.
* **Volume hilang.** CSV tanpa kolom `volume` diterima hanya bila
  `AEGIS_VOLUME_FALLBACK` diisi (mis. volume tick). Tanpa itu berkas ditolak.
* **Replay historis.** `python main.py --csv data/EURUSD_1h.csv --replay`
  menjalankan bar demi bar dengan jam mengikuti bar terakhir, sehingga data lama
  lolos gate kesegaran dan filter sesi membaca jam historis yang benar.
  `--speed N` memajukan N bar per siklus. Progresnya tampil di `/api/state`.
* **Filter sesi & batas harian.** `AEGIS_SESSIONS=07:00-16:00,13:30-20:00`
  (UTC, boleh melewati tengah malam), `AEGIS_SKIP_WEEKEND=true`,
  `AEGIS_MAX_TRADES_PER_DAY=2`. Entry di luar jadwal atau di atas batas
ditahan dan dicatat sebagai `session_closed` / `daily_trade_cap` di audit log.

Catatan jujur: guardrail exposure tetap dihitung sebagai fraksi ekuitas, jadi
forex dengan stop rapat akan sering dibatasi `max_total_exposure` sebelum
risiko per trade tercapai. Itu perilaku pengaman, bukan bug; melonggarkannya
adalah keputusan risiko yang harus disengaja.

## 9d. Kecerdasan analisis dan risiko adaptif

Empat lapisan baru di atas strategi dasar. Semuanya membaca bar yang sudah
tutup, dan `tests.py` memeriksa bahwa bar terakhir yang dipakai memang bar
terakhir yang tertutup.

**1. Konfirmasi multi-timeframe (`intelligence.htf_bias`).** Bar digabung ke
timeframe lebih tinggi (`AEGIS_MTF_FACTOR=4`, jadi 15m → 1h) dan hanya bucket
yang **lengkap** yang dipakai. Bias HTF menjadi satu suara berbobot, dan
melawan bias HTF menaikkan ambang konfluensi +0.05.

**2. Bobot konfluensi per regime.** Bobot tidak lagi tetap: di regime trending,
tren dan HTF lebih berat; di regime ranging, struktur pasar yang lebih berat
(0.24) karena level lebih berarti daripada momentum. Setiap baris bobot
berjumlah 1.00 dan diverifikasi unit test — kalau tidak, ambang
`min_confluence` kehilangan arti.

**3. Divergensi RSI dan pola candle.** Divergensi yang melawan arah menaikkan
ambang +0.10, bukan memblokir entry: setup tetap boleh jalan hanya bila
konfluensinya benar-benar kuat. Pola engulfing/hammer/shooting star menjadi
suara berbobot kecil (0.03–0.06), dan breakout dari kompresi volatilitas
(squeeze) memberi bonus 0.05.

**4. Risiko adaptif (`RiskManager.adaptive_multiplier`).** Ukuran posisi
dikalikan dengan:

| Faktor | Efek |
| --- | --- |
| Kualitas sinyal (skor 0.60–0.90) | ×0.85 sampai ×1.20 |
| ATR persentil (0–1) | sampai ×0.65 pada volatilitas tertinggi |
| Drawdown terhadap batas | potong sampai 50% setelah separuh batas |
| Expectancy 20 trade terakhir | ×0.50 bila negatif |

Hasilnya dijepit di `[AEGIS_RISK_MIN_MULT, AEGIS_RISK_MAX_MULT]` (0.25–1.50) dan
hanya mengalikan `risk_per_trade`. Guardrail keras — exposure total, exposure
satu arah, drawdown, rugi harian — tetap diperiksa sesudahnya dan tidak bisa
ditembus oleh pengali ini.

**Manajemen posisi tambahan.** Stop dipindah ke harga entry setelah
`AEGIS_BREAKEVEN_AFTER_R` (default 1R), lalu trailing jalan seperti biasa.
`AEGIS_MAX_BARS_IN_TRADE` (0 = mati) melepas trade yang tidak bergerak setelah
N bar dan belum mencapai +0.5R — time stop, supaya modal tidak menganggur.
Checklist pra-trade kini 17 poin: tambahan `11_exposure_arah` membatasi
notional satu arah (`AEGIS_DIRECTIONAL_CAP`, default 12% ekuitas) agar dua
posisi searah tidak menumpuk jadi satu taruhan besar.

Semua angka ini terlihat di dashboard: baris **Pengali risiko (adaptif)** dan
**Bias HTF · divergensi · pola** di panel risiko, plus `analysis` dan
`adaptive_risk` di `/api/state`.

## 9e. Eksekusi nyata (opsional, default mati)

`live.py` adalah satu-satunya modul yang bisa mengirim order ke exchange. Ia
hanya dipakai kalau **seluruh** pagar berikut terpenuhi; gagal satu, start
ditolak sebelum ada request apa pun:

| Pagar | Variabel | Default |
| --- | --- | --- |
| Mode live | `AEGIS_LIVE_MODE` | `false` |
| Frasa persetujuan risiko | `AEGIS_LIVE_ACK` = `SAYA PAHAM RISIKONYA` | kosong |
| Kredensial | `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET` | kosong |
| Daftar putih simbol | `AEGIS_LIVE_SYMBOLS` (mis. `BTC/USDT`) | kosong |
| Batas nilai per order | `AEGIS_LIVE_MAX_ORDER_NOTIONAL` | `100` |
| Order market | `AEGIS_LIVE_ALLOW_MARKET` | `false` |
| Kirim sungguhan | `AEGIS_LIVE_DRY_RUN=false` | `true` (dry-run) |

Dengan `AEGIS_LIVE_DRY_RUN=true` (default), setiap order tetap melewati seluruh
validasi — presisi `amount_to_precision`, batas `limits.amount.min`,
`limits.cost.min` dari `load_markets()`, dan batas notional — lalu dicatat ke
audit log sebagai `live_dry_run` **tanpa dikirim**. Itu cara menguji jalur
eksekusi tanpa risiko.

Yang sudah ditangani: `clientOrderId` idempoten (order ganda tidak dikirim
ulang), presisi instrumen dari metadata exchange, batas notional, penolakan
simbol di luar daftar putih, `cancel_all()` saat shutdown, `flatten()` dengan
order `reduceOnly` saat kill-switch, rekonsiliasi order terbuka dan saldo saat
start, dan kredensial yang hanya dibaca dari environment (nilai key tidak
pernah masuk log — hanya sidik `abcd…yz (n karakter)`).

Yang belum ada dan tetap menjadi tanggung jawabmu: manajemen posisi di sisi
exchange saat bot mati mendadak (order SL/TP sudah terpasang, tetapi posisi
tidak diawasi), penanganan fill parsial lanjutan, dan pemilihan waktu eksekusi
yang sadar likuiditas. Uji dengan `dry_run`, lalu nominal kecil.

## 9f. Peta sistem (registri 22 lapisan)

Registri 22 sistem dari dokumen arsitektur dipetakan ke modul nyata di repo ini.
**Telegram (#16) sengaja tidak diikutkan** sesuai permintaan; tidak ada kode
Telegram, token bot, atau dependensi jaringan tambahan.

| # | Sistem | Status | Modul |
| --- | --- | --- | --- |
| 01 | AI Core & Brain | ada | `brain.py` — mesin aturan deterministik, bukan LLM |
| 02 | Data Engine | ada | `data.py` — ccxt publik, CSV, replay, gate kualitas |
| 03 | Market Intelligence | ada | `strategy.py` (`detect_regime`), `intelligence.py` |
| 04 | Technical Analysis | ada | `indicators.py`, `intelligence.py` |
| 05 | Microstructure | ada | `microstructure.py` — book, depth, imbalance, trade flow |
| 06 | On-Chain Intelligence | adaptor | `onchain.py` — butuh data yang kamu suplai (CSV/API) |
| 07 | News & Sentiment | adaptor | `news.py` — leksikon + `RssProvider` |
| 08 | Signal Engine | ada | `strategy.py` — konfluensi berbobot per regime |
| 09 | Risk Engine | ada | `risk.py` — sizing adaptif, 17 checklist, kill switch |
| 10 | Strategy Engine | ada | panel strategi di console + `strategy.py` |
| 11 | Backtesting & Replay | ada | `data.ReplayFeed` + `analytics.py` |
| 12 | Paper Trading | ada | `execution.PaperBroker` |
| 13 | Portfolio Management | ada | `portfolio.py` — bobot, HHI, DCA, rebalancing |
| 14 | AI Evaluation | ada | `evaluation.py` — kalibrasi, Brier, drift |
| 15 | Automation Engine | ada | `monitor.Scheduler` + `deploy/aegis1.service` |
| 16 | Telegram System | **tidak diikutkan** | — |
| 17 | Voice System | ada | `voice.py` — narasi + TTS opsional (`espeak`/`say`) |
| 18 | Web Dashboard | ada | `dashboard.py` + `static/index.html` |
| 19 | Database & Memory | ada | `logger.py` (SQLite + JSONL) + migrasi kolom |
| 20 | Security & Reliability | ada | `security.py` — hash chain, rate limit, pemindai rahasia |
| 21 | Monitoring & Recovery | ada | `monitor.py` — health, heartbeat, backup/restore |
| 22 | Deployment & Testing | ada | `Dockerfile`, `.github/workflows/ci.yml`, 122 test |

Endpoint baru: `GET /api/briefing` (situasi, bukti, keputusan, keyakinan),
`GET /api/health` (umur tick, error beruntun, disk, heartbeat),
`GET /api/portfolio` (nilai, bobot, konsentrasi, PnL). Panel **Briefing** di
console menampilkan hasilnya.

Batas yang tetap jujur: `onchain.py` dan `news.py` tidak menarik data sendiri
saat diuji dan tidak punya sumber gratis bawaan — keduanya butuh provider
(CSV, RSS, atau API milikmu). Kalau tidak ada provider, keduanya melaporkan
"belum ada data" dan tidak mengarang angka.

## 10. Uji

```bash
python tests.py            # 122 test
pytest tests.py -q         # sama, lewat pytest
```

Yang diuji antara lain: sizing 1% benar-benar 1% saat batas tidak mengikat,
pembulatan selalu ke bawah, exposure/leverage/slot membatasi, batas rugi harian
dan drawdown termasuk unrealized, 16 checklist menolak pada tiap kondisi tidak
aman (kill, pause, spread lebar, gate gagal, order pending, bar duplikat,
cooldown), sinyal menolak data kurang dan RSI ekstrem, sinyal actionable selalu
RR ≥ 2 dan stop di sisi yang benar, gate menolak data basi/duplikat/spread lebar,
eksekusi menolak order ganda dan qty di bawah minimum, stop terpicu saat harga
melewati level, limit order kedaluwarsa, dan `LIVE_MODE=true` **ditolak**.

## 11. Berjalan 24/7 di VPS

```bash
sudo useradd -r -s /bin/false aegis
sudo mkdir -p /opt/aegis1 && sudo chown aegis:aegis /opt/aegis1
# salin proyek, buat venv, install requirements
sudo cp deploy/aegis1.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aegis1
sudo journalctl -u aegis1 -f
```

`Restart=always` + `RestartSec=5` menangani crash; `KillSignal=SIGTERM` memicu
shutdown rapi (loop berhenti, state terakhir tersimpan). Jangan menjalankan
proses dengan `&`/`nohup` di luar supervisor — tidak ada yang me-restart-nya.

## 12. Alert

Saat ini alert ditulis ke audit log dan stdout: startup, shutdown, entry, exit,
mode change, drawdown, batas rugi harian, kill-switch, error siklus. Titik
sambungnya jelas: `Alerts.subscribe(fn)` di `logger.py`. Kanal eksternal
(Telegram/Discord/email) sengaja tidak disertakan — itu di luar cakupan paket ini
dan butuh token bot yang tidak boleh disimpan di repo. Bila Anda menambahkannya,
simpan token di environment/secret manager, bukan di kode.

---

## Batas paket dan langkah menuju live

Bagian ini yang paling penting untuk dibaca.

**Yang ada di paket ini.** Seluruh logika sinyal, risiko, sizing, manajemen
posisi, model biaya, audit, dashboard, kill-switch, dan 122 test. Mesin berjalan
penuh dan bisa dipakai untuk mengukur perilaku strategi atas data apa pun.

**Yang tidak ada, dengan sengaja.**

1. **Eksekusi nyata ada, tetapi default-nya mati.** `live.LiveBroker` bisa
   mengirim order ke exchange, dan hanya dibangun bila seluruh pagar terpenuhi
   (`AEGIS_LIVE_MODE`, frasa `AEGIS_LIVE_ACK`, kredensial, daftar putih simbol,
   batas notional) — lihat §9e. `AEGIS_LIVE_DRY_RUN=true` adalah default: order
   divalidasi dan dicatat, tidak dikirim. Tanpa pagar lengkap, `build_broker()`
   melempar `LiveBrokerUnavailable` sebelum menyentuh jaringan.
2. **Tidak ada kanal alert eksternal.** Peringatan hanya ditulis ke audit log
   dan stdout (`logger.Alerts`); tidak ada SMTP, Discord, Slack, webhook, atau
   Telegram. Telegram sengaja tidak diikutkan (lihat §9f). Titik sambungnya
   jelas: `Alerts.subscribe`.
3. **Tidak ada jaminan profit, dan tidak ada klaim profit.** Seluruh angka di
   dashboard, laporan, dan demo adalah hasil simulasi dengan fee, slippage, dan
   spread eksplisit.

**Kontrak yang harus dipenuhi bila Anda membangun adapter live sendiri.**
`execution.Broker` adalah protokolnya: `balance()`, `place()`, `cancel()`,
`modify()`, `open_orders()`, `all_orders()`, `on_price()`. Untuk layak dipakai
dengan uang nyata, minimal:

- baca kredensial dari environment saja, rotasi key, izin trade-only tanpa
  penarikan;
- idempotensi berbasis `client_id` dan `signal_id` (paket ini sudah menolak
  duplikat; adapter live harus memakai `newClientOrderId` exchange, bukan
  mengandalkan ingatan proses);
- rekonsiliasi posisi nyata terhadap database saat start — jangan berasumsi
  database adalah kebenaran;
- tangani rate limit (`429`), timeout, `5xx`, reconnect WebSocket, partial fill,
  dan posisi yang sudah tertutup di exchange tetapi masih terbuka di memori;
- verifikasi bahwa SL/TP benar-benar terpasang di exchange sebelum menganggap
  posisi terlindungi; bila gagal, tutup posisi dan kirim alert;
- uji di testnet exchange selama minimal beberapa minggu, lalu modal minimum
  yang Anda siap kehilangan seluruhnya;
- jangan melewati 16 checklist pra-trade dan gate kualitas data — itu bagian dari
  kontrak, bukan hiasan.

**Langkah aman sebelum uang sungguhan.**

1. `python tests.py` — semua hijau.
2. `python main.py --offline` beberapa jam; periksa `events`, `orders`,
   `positions` di SQLite. Pastikan tidak ada order tanpa SL.
3. Ulangi dengan `--csv` data nyata beberapa bulan, termasuk periode crash.
4. Baca distribusi R dan `close_reason`; kalau mayoritas exit adalah `stop_loss`,
   masalahnya di entry atau di stop, bukan di target.
5. Periksa biaya: fee + slippage pada log sering lebih besar dari dugaan.
6. Baru setelah itu pertimbangkan adapter live, dengan batas risiko yang sama
   atau lebih ketat, dan modal yang siap hilang.

---

## Lisensi dan tanggung jawab

Kode open-source untuk dipelajari dan diubah. Trading membawa risiko kehilangan
modal. Tidak ada bagian dari paket ini yang merupakan nasihat keuangan.
