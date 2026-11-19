# OpenCode Unlimited Bridge

OpenAI-compatible local gateway (`:20900`) + rotating HTTP/SOCKS5 proxy relay (`:20988`) untuk menjalankan model gratis OpenCode (`opencode/*`) secara paralel dengan failover otomatis.

---

## Arsitektur

```text
Client / Router (:12800 / :20800)
        │  OpenAI / Anthropic API (/v1/chat/completions, /v1/messages)
        ▼
  oc_bridge.py (:20900)
   ├──► Primary: opencode serve (:20999) ──► rotating_proxy_relay.py (:20988) ──► HTTP/SOCKS5 Pool / Direct
   └──► Failover: opencode run --standalone (stdin prompt, rotasi proxy + cooldown 429)
```

- **`scripts/rotating_proxy_relay.py` (`127.0.0.1:20988`)**  
  Proxy HTTP/CONNECT lokal yang merotasi daftar proxy HTTP & SOCKS5 dari `state/oc_verified_proxies.txt` dan `state/private_proxies.txt`, lengkap dengan cooldown otomatis saat terkena `402/407/429` dan fallback ke koneksi langsung.
- **`opencode serve` (`127.0.0.1:20999`)**  
  Hot daemon OpenCode yang berjalan persisten di belakang relay `:20988` agar request tidak mengalami cold-start CLI.
- **`scripts/oc_bridge.py` (`127.0.0.1:20900`)**  
  Bridge OpenAI-compatible (`/v1/models`, `/v1/chat/completions`, `/v1/messages`) dengan dukungan streaming SSE maupun non-streaming. Saat failover ke CLI, prompt dikirim melalui `stdin` agar aman untuk konteks panjang (bebas `Errno 7 Argument list too long`).
- **`scripts/oc_proxy_refresh.py`**  
  Scanner & validator proxy otomatis untuk mengisi `state/oc_verified_proxies.txt`.

---

## Prasyarat

- **Python 3.10+** (tanpa dependency eksternal — full Python standard library).
- **OpenCode CLI** terpasang dan ada di `PATH` (atau set `OPENCODE_BIN=/path/to/opencode`).

---

## Cara Install & Jalankan Cepat

```bash
git clone git@github.com:Alstore01/opencode-unlimited.git
cd opencode-unlimited
cp .env.example .env
mkdir -p state
```

Edit `.env` sesuai kebutuhan (minimal ubah `OPENCODE_SERVER_PASSWORD` dan `OPENCODE_BIN` jika perlu), lalu tambahkan proxy kamu ke `state/private_proxies.txt` (opsional, disarankan pakai proxy private/residential agar tidak cepat terkena `FreeUsageLimitError`):

Format proxy yang didukung (satu per baris):
```text
http://user:pass@host:port
socks5h://user:pass@host:port
http://host:port
host:port:user:pass
host:port
```

Jalankan semua komponen sekaligus:
```bash
chmod +x start.sh
./start.sh
```

Atau jalankan di background sebagai systemd service menggunakan template di folder `systemd/`.

---

## Refresh Proxy Publik (Opsional)

Untuk memindai proxy publik & menyaring yang masih tembus OpenCode:
```bash
python3 -u scripts/oc_proxy_refresh.py
```
Hasilnya otomatis disimpan ke `state/oc_verified_proxies.txt` dan akan di-reload setiap 30 detik oleh `rotating_proxy_relay.py` tanpa perlu restart.

---

## Cara Pakai API

### 1. Cek Daftar Model
```bash
curl -s http://127.0.0.1:20900/v1/models | jq .
```

### 2. Chat Completion (cURL)
```bash
curl -X POST http://127.0.0.1:20900/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "big-pickle",
    "messages": [{"role": "user", "content": "Halo, tes singkat!"}],
    "stream": false
  }'
```

### 3. Python OpenAI SDK
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:20900/v1",
    api_key="any-key",
)

resp = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "Ringkas konsep async/await."}],
)
print(resp.choices[0].message.content)
```

### Model ID yang Didukung
`beam`, `big-pickle`, `deepseek-v4.1-flash`, `flint-40b`, `hy4-flash`, `iceman`, `inkling-2-small`, `longcat-2.5`, `mimo-v2.6-flash`, `nemotron-3.5-lightning`, `nemotron-3.5-super-vl`.
