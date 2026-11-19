#!/usr/bin/env python3
"""OpenCode Rotating Proxy Relay (HTTP + Basic Auth + SOCKS5 + Direct fallback).

Listens on 127.0.0.1:20988 as a plain HTTP/CONNECT proxy for opencode-daemon.
Supports upstream HTTP proxies (with optional Basic auth), SOCKS5 proxies,
cooldown tracking, hot-reloading, and seamless direct fallback when no verified
working proxy is available so requests never stall on dead 402/407 proxies.
"""
import base64
import os
import random
import re
import select
import socket
import struct
import threading
import time
import urllib.parse

LISTEN = int(os.environ.get("LISTEN_PORT", "20988"))
PROXY_FILES = [
    os.environ.get("OC_VERIFIED_PROXIES", os.environ.get("OC_STATE_DIR", "./state") + "/oc_verified_proxies.txt"),
    os.environ.get("PRIVATE_PROXY_FILE", os.environ.get("OC_STATE_DIR", "./state") + "/private_proxies.txt"),
]
RELOAD_INTERVAL = 30.0
COOLDOWN_SEC = 600.0

LOCK = threading.Lock()
PROXIES = []
COOLDOWNS = {}  # key -> expire_ts
LAST_LOAD = 0.0
LAST_LOG_SIGNATURE = None


def parse_proxy(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        u = urllib.parse.urlparse(line)
        if not u.hostname or not u.port:
            return None
        scheme = u.scheme.lower()
        if scheme.startswith("socks5"):
            proto = "socks5"
        elif scheme in ("http", "https"):
            proto = "http"
        else:
            return None
        return {
            "key": f"{proto}://{u.hostname}:{u.port}",
            "proto": proto,
            "host": u.hostname,
            "port": int(u.port),
            "user": urllib.parse.unquote(u.username or ""),
            "pass": urllib.parse.unquote(u.password or ""),
        }
    parts = line.split(":")
    if len(parts) == 4 and parts[1].isdigit():
        return {
            "key": f"http://{parts[0]}:{parts[1]}",
            "proto": "http",
            "host": parts[0],
            "port": int(parts[1]),
            "user": parts[2],
            "pass": parts[3],
        }
    if len(parts) == 2 and parts[1].isdigit():
        return {
            "key": f"http://{parts[0]}:{parts[1]}",
            "proto": "http",
            "host": parts[0],
            "port": int(parts[1]),
            "user": "",
            "pass": "",
        }
    return None


def load_proxies(force: bool = False):
    global PROXIES, LAST_LOAD, LAST_LOG_SIGNATURE
    now = time.time()
    if not force and (now - LAST_LOAD) < RELOAD_INTERVAL:
        return
    loaded = []
    seen = set()
    for fpath in PROXY_FILES:
        if not fpath or not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    p = parse_proxy(raw)
                    if p and p["key"] not in seen:
                        seen.add(p["key"])
                        loaded.append(p)
        except Exception:
            pass
    with LOCK:
        PROXIES = loaded
        LAST_LOAD = now
        sig = len(loaded)
        if sig != LAST_LOG_SIGNATURE:
            LAST_LOG_SIGNATURE = sig
            mode = f"{sig} verified proxies" if sig > 0 else "direct-clean mode (0 verified external proxies)"
            print(f"[relay] Pool updated: {mode}", flush=True)


def pick_proxy():
    load_proxies()
    now = time.time()
    with LOCK:
        healthy = [p for p in PROXIES if COOLDOWNS.get(p["key"], 0.0) <= now]
    if not healthy:
        return None
    return random.choice(healthy)


def mark_cooldown(p, seconds: float = COOLDOWN_SEC):
    with LOCK:
        COOLDOWNS[p["key"]] = time.time() + seconds


def is_localhost(host: str) -> bool:
    h = host.lower().strip("[]")
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def dial_direct(dst_host: str, dst_port: int, timeout: float = 15.0) -> socket.socket:
    """Connect directly, preferring IPv4 first so shared-IPv6 OpenCode quota doesn't block IPv4."""
    last_err = None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(dst_host, dst_port, family, socket.SOCK_STREAM)
        except OSError as e:
            last_err = e
            continue
        for af, socktype, proto, _, sa in infos:
            s = socket.socket(af, socktype, proto)
            s.settimeout(timeout)
            try:
                s.connect(sa)
                return s
            except OSError as e:
                last_err = e
                try:
                    s.close()
                except OSError:
                    pass
    if last_err:
        raise last_err
    return socket.create_connection((dst_host, dst_port), timeout=timeout)


def dial_via_proxy(proxy, dst_host: str, dst_port: int, method: str, raw_head: bytes):
    up = socket.create_connection((proxy["host"], proxy["port"]), timeout=4)
    up.settimeout(12)
    try:
        if proxy["proto"] == "socks5":
            user = proxy["user"].encode()
            pw = proxy["pass"].encode()
            if user or pw:
                up.sendall(b"\x05\x02\x00\x02")
                resp = up.recv(2)
                if len(resp) < 2 or resp[0] != 5:
                    raise RuntimeError("invalid socks5 greeting")
                if resp[1] == 2:
                    up.sendall(bytes([1, len(user)]) + user + bytes([len(pw)]) + pw)
                    auth_r = up.recv(2)
                    if len(auth_r) < 2 or auth_r[1] != 0:
                        raise RuntimeError("socks5 auth failed")
                elif resp[1] != 0:
                    raise RuntimeError("socks5 auth rejected")
            else:
                up.sendall(b"\x05\x01\x00")
                resp = up.recv(2)
                if resp != b"\x05\x00":
                    raise RuntimeError("socks5 no-auth rejected")
            hb = dst_host.encode("idna")
            up.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", dst_port))
            r = up.recv(4)
            if len(r) < 4 or r[1] != 0:
                raise RuntimeError(f"socks5 connect status {r[1] if len(r) >= 2 else -1}")
            atyp = r[3]
            if atyp == 1:
                up.recv(6)
            elif atyp == 3:
                up.recv(up.recv(1)[0] + 2)
            elif atyp == 4:
                up.recv(18)
            if method != "CONNECT":
                up.sendall(raw_head)
            return up

        # HTTP proxy
        auth_hdr = ""
        if proxy["user"] or proxy["pass"]:
            tok = base64.b64encode(f"{proxy['user']}:{proxy['pass']}".encode()).decode()
            auth_hdr = f"Proxy-Authorization: Basic {tok}\r\n"
        if method == "CONNECT":
            req = f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\nHost: {dst_host}:{dst_port}\r\n{auth_hdr}Proxy-Connection: Keep-Alive\r\n\r\n".encode()
            up.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp and len(resp) < 8192:
                chunk = up.recv(4096)
                if not chunk:
                    break
                resp += chunk
            first = resp.split(b"\r\n", 1)[0].decode(errors="ignore")
            if " 200" not in first:
                raise RuntimeError(f"http proxy CONNECT rejected: {first}")
            return up
        else:
            first, _, rest = raw_head.partition(b"\r\n")
            up.sendall(first + b"\r\n" + auth_hdr.encode() + rest)
            return up
    except Exception:
        try:
            up.close()
        except Exception:
            pass
        raise


def pipe(a: socket.socket, b: socket.socket):
    a.settimeout(None)
    b.settimeout(None)
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def handle(client: socket.socket):
    up = None
    try:
        client.settimeout(15)
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 32768:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            head += chunk
        first_line = head.split(b"\r\n", 1)[0].decode(errors="ignore")
        parts = first_line.split()
        if len(parts) < 2:
            client.close()
            return
        method = parts[0].upper()
        target = parts[1]

        if method == "CONNECT":
            if ":" in target:
                dst_host, ps = target.rsplit(":", 1)
                dst_host = dst_host.strip("[]")
                dst_port = int(ps)
            else:
                dst_host, dst_port = target, 443
        else:
            u = urllib.parse.urlparse(target)
            dst_host = u.hostname or ""
            dst_port = u.port or (443 if u.scheme == "https" else 80)
            if not dst_host:
                m = re.search(rb"(?i)^Host:\s*([^\r\n]+)", head, re.M)
                if m:
                    hval = m.group(1).decode(errors="ignore").strip()
                    if ":" in hval:
                        dst_host, ps = hval.rsplit(":", 1)
                        dst_port = int(ps)
                    else:
                        dst_host = hval

        if not dst_host:
            client.close()
            return

        if is_localhost(dst_host):
            if dst_port == LISTEN:
                if method == "GET" and target in ("/health", "/ping", "/"):
                    client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"status\":\"ok\",\"service\":\"oc-proxy-relay\"}\r\n")
                client.close()
                return
            up = socket.create_connection((dst_host, dst_port), timeout=15)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                up.sendall(head)
            pipe(client, up)
            return

        # Try up to 3 verified proxies first
        for _ in range(3):
            p = pick_proxy()
            if not p:
                break
            try:
                up = dial_via_proxy(p, dst_host, dst_port, method, head)
                if method == "CONNECT":
                    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                pipe(client, up)
                return
            except Exception:
                mark_cooldown(p)
                up = None

        # Clean direct fallback when no working verified proxy exists (prefer IPv4 first)
        up = dial_direct(dst_host, dst_port, timeout=15)
        if method == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            # Rewrite absolute proxy URL to path-relative origin HTTP request
            u = urllib.parse.urlparse(target)
            rel_path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            ver = parts[2] if len(parts) > 2 else "HTTP/1.1"
            _, _, rest = head.partition(b"\r\n")
            rest = re.sub(rb"(?im)^Proxy-Authorization:[^\r\n]*\r\n", b"", rest)
            rest = re.sub(rb"(?im)^Proxy-Connection:[^\r\n]*\r\n", b"", rest)
            up.sendall(f"{method} {rel_path} {ver}\r\n".encode() + rest)
        pipe(client, up)
    except Exception:
        try:
            client.close()
        except OSError:
            pass
        if up:
            try:
                up.close()
            except OSError:
                pass


def main():
    load_proxies(force=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN))
    srv.listen(256)
    print(f"[relay] Listening on 127.0.0.1:{LISTEN}", flush=True)
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
