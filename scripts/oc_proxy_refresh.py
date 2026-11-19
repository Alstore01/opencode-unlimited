#!/usr/bin/env python3
"""Validate and refresh working OpenCode-ready proxies (HTTP + SOCKS5 + private).

Tests candidate proxies from private_proxies.txt, floppydata_proxies.txt (if alive),
and fresh HTTP/SOCKS5 lists first against https://opencode.ai/favicon.ico, and saves
reachable fast proxies + proxies verified end-to-end against OpenCode CLI without
FreeUsageLimitError into /root/.hermes/state/oc_verified_proxies.txt and
/root/.hermes/state/fast_proxies.txt.
"""
import concurrent.futures
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

STATE_DIR = os.environ.get("OC_STATE_DIR", "./state")
PRIVATE_FILE = f"{STATE_DIR}/private_proxies.txt"
FLOPPY_FILE = f"{STATE_DIR}/floppydata_proxies.txt"
WORKING_FILE = f"{STATE_DIR}/working_proxies.txt"
FAST_FILE = f"{STATE_DIR}/fast_proxies.txt"
VERIFIED_OC_FILE = f"{STATE_DIR}/oc_verified_proxies.txt"
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode")

PUBLIC_SOURCES = [
    ("socks5h", "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/socks5.txt"),
    ("socks5h", "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"),
    ("socks5h", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt"),
    ("socks5h", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt"),
    ("socks5h", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("http", "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt"),
    ("http", "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/https.txt"),
    ("http", "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=yes"),
]


def normalize_proxy(line: str, default_proto: str = "http") -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        return f"{default_proto}://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"{default_proto}://{parts[0]}:{parts[1]}"
    return None


def gather_candidates(include_public: bool = True) -> list[str]:
    seen = set()
    out = []
    for path in (PRIVATE_FILE, WORKING_FILE, FLOPPY_FILE):
        if os.path.exists(path):
            for l in open(path, encoding="utf-8", errors="ignore"):
                p = normalize_proxy(l)
                if p and p not in seen:
                    seen.add(p)
                    out.append(p)
    if include_public:
        def _fetch(item):
            proto, url = item
            res = []
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "curl/8.5"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    for raw in r.read().decode("utf-8", "ignore").splitlines():
                        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", raw)
                        if m:
                            res.append(f"{proto}://{m.group(1)}:{m.group(2)}")
            except Exception:
                pass
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for chunk in ex.map(_fetch, PUBLIC_SOURCES):
                for p in chunk:
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
    return out


def check_reachability(proxy_url: str):
    t0 = time.time()
    try:
        r = subprocess.run(
            ["curl", "-s", "-m", "3", "--connect-timeout", "2.5", "-x", proxy_url,
             "-o", "/dev/null", "-w", "%{http_code}", "https://opencode.ai/favicon.ico"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() in ("200", "204", "301", "302", "404"):
            return (round(time.time() - t0, 2), proxy_url)
    except Exception:
        pass
    return None


def start_socks5_http_bridge(socks_url: str):
    u = urllib.parse.urlparse(socks_url)
    sh, sp = u.hostname, int(u.port or 1080)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def handle(cli):
        up = None
        try:
            cli.settimeout(10)
            req = b""
            while b"\r\n\r\n" not in req and len(req) < 8192:
                c = cli.recv(4096)
                if not c:
                    return
                req += c
            first = req.split(b"\r\n", 1)[0].decode().split()
            host, dps = first[1].rsplit(":", 1)
            up = socket.create_connection((sh, sp), timeout=6)
            up.sendall(b"\x05\x01\x00")
            if up.recv(2) != b"\x05\x00":
                return
            hb = host.encode()
            up.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", int(dps)))
            r = up.recv(4)
            if len(r) < 4 or r[1] != 0:
                return
            if r[3] == 1:
                up.recv(6)
            elif r[3] == 3:
                up.recv(up.recv(1)[0] + 2)
            elif r[3] == 4:
                up.recv(18)
            cli.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            cli.settimeout(None)
            up.settimeout(None)
            while True:
                rr, _, _ = select.select([cli, up], [], [], 25)
                if not rr:
                    break
                for s in rr:
                    d = s.recv(32768)
                    if not d:
                        return
                    (up if s is cli else cli).sendall(d)
        except Exception:
            pass
        finally:
            try:
                cli.close()
            except Exception:
                pass
            if up:
                try:
                    up.close()
                except Exception:
                    pass

    import select
    def loop():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                threading.Thread(target=handle, args=(c,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    threading.Thread(target=loop, daemon=True).start()
    return port, srv, stop


def test_opencode_clean(proxy_url: str) -> bool:
    import shutil
    import tempfile
    bridge = None
    temp_home = tempfile.mkdtemp(prefix="oc_probe_")
    http_proxy = proxy_url
    try:
        if proxy_url.startswith("socks5"):
            port, srv, stop = start_socks5_http_bridge(proxy_url)
            bridge = (srv, stop)
            http_proxy = f"http://127.0.0.1:{port}"
        os.makedirs("/tmp/oc_empty", exist_ok=True)
        env = os.environ.copy()
        env["HOME"] = temp_home
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["HTTP_PROXY"] = http_proxy
        env["HTTPS_PROXY"] = http_proxy
        env["http_proxy"] = http_proxy
        env["https_proxy"] = http_proxy
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
        env["no_proxy"] = "localhost,127.0.0.1,::1"
        r = subprocess.run(
            [OPENCODE_BIN, "run", "--standalone", "--model", "opencode/big-pickle", "--format", "json", "OK"],
            cwd="/tmp/oc_empty", env=env, capture_output=True, text=True, timeout=18,
        )
        if r.returncode != 0:
            return False
        for l in r.stdout.splitlines():
            try:
                if json.loads(l).get("type") == "text":
                    return True
            except Exception:
                pass
    except Exception:
        pass
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)
        if bridge:
            srv, stop = bridge
            stop.set()
            try:
                srv.close()
            except Exception:
                pass
    return False


def main():
    cands = gather_candidates(include_public=True)
    # Cap public scan per run so refresh completes within ~20s cleanly
    priv = [c for c in cands if "@" in c]
    pub = [c for c in cands if "@" not in c][:1500]
    scan_list = priv[:200] + pub
    print(f"[oc_proxy_refresh] scanning {len(scan_list)} candidates (priv={len(priv[:200])}, pub={len(pub)})...", flush=True)

    reachable = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=150) as ex:
        for res in ex.map(check_reachability, scan_list):
            if res:
                reachable.append(res)
    reachable.sort()
    fast = [p for _, p in reachable[:150]]
    print(f"[oc_proxy_refresh] reachable opencode.ai={len(reachable)}", flush=True)

    with open(FAST_FILE + ".tmp", "w") as f:
        f.write("\n".join(fast) + ("\n" if fast else ""))
    os.replace(FAST_FILE + ".tmp", FAST_FILE)

    # End-to-end check up to 40 fastest proxies (parallel 6x) for non-429 OpenCode quota
    verified_oc = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for p, ok in zip(fast[:40], ex.map(test_opencode_clean, fast[:40])):
            if ok:
                verified_oc.append(p)
                print(f"  [VERIFIED_200] {p}", flush=True)
    with open(VERIFIED_OC_FILE + ".tmp", "w") as f:
        f.write("\n".join(verified_oc) + ("\n" if verified_oc else ""))
    os.replace(VERIFIED_OC_FILE + ".tmp", VERIFIED_OC_FILE)

    print(f"[oc_proxy_refresh] saved fast={len(fast)} verified_oc={len(verified_oc)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
