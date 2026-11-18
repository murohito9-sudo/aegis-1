# Research

Tempat mencatat hipotesis, dataset, dan experiment. Tujuannya satu: setiap hasil bisa ditelusuri kembali ke data, kode, dan parameter yang menghasilkannya.

```
research/
├── hypotheses/   # HYP-NNN — pertanyaan yang mau diuji
├── datasets/     # DATASET-NNN — versi data yang dipakai (metadata, bukan data mentah)
├── experiments/  # EXP-NNN — satu eksekusi dengan parameter tertentu
└── reports/      # rangkuman hasil, termasuk yang gagal
```

## Urutan kerja

1. Tulis hipotesis (`HYP-NNN`) sebelum menyentuh kode strategi.
2. Bekukan dataset version yang dipakai.
3. Jalankan experiment (`EXP-NNN`), catat commit + parameter + biaya + slippage.
4. Laporkan hasil apa adanya, termasuk yang buruk.
5. Hipotesis yang ditolak dicatat beserta alasannya, supaya tidak diulang tanpa alasan baru.

## Aturan

- Jangan melaporkan periode/aset/parameter terbaik saja. Kalau melakukan seleksi, catat alasan dan seluruh search space.
- Hasil tanpa dataset version, strategy version, dan status out-of-sample ditandai `Unverified`.
- Jangan menyalin data mentah ke repo. Yang disimpan adalah metadata dan cara mengambilnya.
- Jangan menulis kredensial apa pun di folder ini.
