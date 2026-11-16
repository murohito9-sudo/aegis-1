# AEGIS-1 — image runtime. Default tetap paper; live hanya lewat env eksplisit.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Lapisan dependensi dipisah agar rebuild cepat.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Jalankan sebagai pengguna biasa, bukan root.
RUN useradd --create-home --uid 10001 aegis \
    && mkdir -p /app/data \
    && chown -R aegis:aegis /app
USER aegis

# Dashboard hanya mendengarkan di loopback di dalam container; akses dari luar
# lewat SSH tunnel atau reverse proxy + autentikasi, bukan port terbuka.
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "main.py"]
