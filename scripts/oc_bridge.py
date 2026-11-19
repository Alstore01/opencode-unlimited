#!/usr/bin/env python3
"""OpenCode Unlimited Bridge for MahiCenter Router (Port 20900).

Routes OpenAI/Anthropic requests for verified live OpenCode free models:
  1. Primary: Persistent warm `opencode serve` daemon on 127.0.0.1:20999
     (egress routed through rotating proxy relay 127.0.0.1:20988)
  2. Failover: Standalone `opencode run --standalone --format json` rotating
     verified HTTP/SOCKS5/private proxies + clean direct fallback
  3. Exact-model failover ONLY for identical upstream weights
     (e.g. glm-5.3-flash -> dahl zai-org/GLM-5.3-Flash) — zero spoofing.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "20900"))
DAEMON_URL = os.environ.get("OPENCODE_DAEMON_URL", "http://127.0.0.1:20999").rstrip("/")
DAEMON_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", os.environ.get("OPENCODE_SERVER_PASSWORD", "opencode-local-daemon"))
OPENCODE_BIN = os.environ.get("OPENCODE_BIN", os.environ.get("OPENCODE_BIN", "opencode"))
EMPTY_WORKDIR = "/tmp/oc_empty"
CLI_HOME = "/tmp/oc_bridge_cli"

DAEMON_TIMEOUT = float(os.environ.get("OC_DAEMON_TIMEOUT", "55"))
CLI_TIMEOUT = float(os.environ.get("OC_CLI_TIMEOUT", "45"))

STATE_DIR = os.environ.get("OC_STATE_DIR", "./state")
PROXY_SOURCES = [
    os.environ.get("OC_VERIFIED_PROXIES", f"{STATE_DIR}/oc_verified_proxies.txt"),
    os.environ.get("PRIVATE_PROXY_FILE", f"{STATE_DIR}/private_proxies.txt"),
]
DAHL_KEYS_FILE = f"{STATE_DIR}/dahl_keys.json"

os.makedirs(EMPTY_WORKDIR, exist_ok=True)
os.makedirs(CLI_HOME, exist_ok=True)

MODEL_MAP = {
    "beam": "opencode/beam-free",
    "big-pickle": "opencode/big-pickle",
    "deepseek-v4.1-flash": "opencode/deepseek-v4.1-flash-free",
    "flint-40b": "opencode/flint-40b-free",
    "hy4-flash": "opencode/hy4-flash-free",
    "iceman": "opencode/iceman-free",
    "inkling-2-small": "opencode/inkling-2-small-free",
    "longcat-2.5": "opencode/longcat-2.5-free",
    "mimo-v2.6-flash": "opencode/mimo-v2.6-flash-free",
    "nemotron-3.5-lightning": "opencode/nemotron-3.5-lightning-free",
    "nemotron-3.5-super-vl": "opencode/nemotron-3.5-super-vl-free",
}

SEM = threading.Semaphore(int(os.environ.get("OC_MAX_CONCURRENCY", "8")))
PROXY_LOCK = threading.Lock()
PROXY_COOLDOWNS: dict[str, float] = {}
COOLDOWN_SEC = 900.0


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [oc-bridge] {msg}", flush=True)


def resolve_model(raw: str | None) -> str | None:
    if not raw:
        return "deepseek-v4.1-flash"
    name = raw.strip()
    for prefix in ("mahi/", "oc/", "opencode/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    low = name.lower()
    if low in MODEL_MAP:
        return low
    for suffix in (":free", "-free"):
        if low.endswith(suffix):
            cand = low[:-len(suffix)]
            if cand in MODEL_MAP:
                return cand
    return None


def _load_proxy_candidates() -> list[str]:
    now = time.time()
    seen: set[str] = set()
    cands: list[str] = []
    with PROXY_LOCK:
        for k, exp in list(PROXY_COOLDOWNS.items()):
            if exp <= now:
                del PROXY_COOLDOWNS[k]
        cooled = set(PROXY_COOLDOWNS.keys())

    for path in PROXY_SOURCES:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "://" not in line:
                        parts = line.split(":")
                        if len(parts) == 4 and parts[1].isdigit():
                            line = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                        elif len(parts) == 2 and parts[1].isdigit():
                            line = f"http://{parts[0]}:{parts[1]}"
                        else:
                            continue
                    if line not in seen and line not in cooled:
                        seen.add(line)
                        cands.append(line)
        except Exception:
            pass
    return cands


def _cool_proxy(proxy: str, seconds: float = COOLDOWN_SEC) -> None:
    if not proxy:
        return
    with PROXY_LOCK:
        PROXY_COOLDOWNS[proxy] = time.time() + seconds


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                itype = item.get("type", "")
                if itype == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif itype == "input_text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif itype == "tool_result":
                    parts.append(f"[ToolResult] {_flatten_content(item.get('content', ''))}")
                elif itype == "tool_use":
                    parts.append(f"[ToolUse {item.get('name','')}] {json.dumps(item.get('input', {}))}")
        return "\n".join(p for p in parts if p)
    return str(content or "")


def extract_prompt(req: dict, is_anthropic: bool = False) -> str:
    sections: list[str] = []
    if is_anthropic and req.get("system"):
        sys_txt = _flatten_content(req["system"])
        if sys_txt.strip():
            sections.append(f"[System]\n{sys_txt.strip()}")

    msgs = req.get("messages")
    if isinstance(msgs, list) and msgs:
        if len(msgs) == 1 and not sections and (msgs[0].get("role") == "user"):
            return _flatten_content(msgs[0].get("content", "")).strip() or "Hello"
        for m in msgs:
            role = str(m.get("role") or "user").strip()
            txt = _flatten_content(m.get("content", "")).strip()
            if txt:
                if role == "system":
                    sections.append(f"[System]\n{txt}")
                elif role == "assistant":
                    sections.append(f"[Assistant]\n{txt}")
                elif role == "tool":
                    sections.append(f"[Tool]\n{txt}")
                else:
                    sections.append(f"[User]\n{txt}")
        return "\n\n".join(sections).strip() or "Hello"

    if isinstance(req.get("prompt"), str) and req["prompt"].strip():
        return req["prompt"].strip()
    return "Hello"


def _daemon_req(path: str, method: str = "GET", body: dict | None = None, timeout: float = 15.0) -> tuple[int, dict | list | str]:
    auth = base64.b64encode(f"opencode:{DAEMON_PASSWORD}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{DAEMON_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def run_via_daemon(oc_model: str, prompt: str) -> tuple[str | None, str, dict, str]:
    """Run prompt against warm persistent opencode serve daemon."""
    raw_id = oc_model.split("/", 1)[1] if "/" in oc_model else oc_model
    sid = None
    t0 = time.time()
    try:
        st, create_obj = _daemon_req(
            "/api/session",
            "POST",
            {"model": {"id": raw_id, "providerID": "opencode"}},
            timeout=10.0,
        )
        if st != 200 or not isinstance(create_obj, dict):
            return None, "", {}, f"daemon create HTTP {st}"
        data = create_obj.get("data") if isinstance(create_obj.get("data"), dict) else create_obj
        sid = data.get("id")
        if not sid:
            return None, "", {}, "daemon create missing id"

        st_p, _ = _daemon_req(f"/api/session/{sid}/prompt", "POST", {"text": prompt}, timeout=15.0)
        if st_p != 200:
            return None, "", {}, f"daemon prompt HTTP {st_p}"

        deadline = t0 + DAEMON_TIMEOUT
        last_err = ""
        while time.time() < deadline:
            time.sleep(0.2)
            st_m, msgs_obj = _daemon_req(f"/api/session/{sid}/message", "GET", timeout=10.0)
            if st_m != 200 or not isinstance(msgs_obj, dict):
                continue
            mlist = msgs_obj.get("data", [])
            if not isinstance(mlist, list):
                continue

            is_idle = False
            idle_outcome = ""
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tokens: dict = {}
            err_msg = ""

            for m in mlist:
                if not isinstance(m, dict):
                    continue
                mtype = m.get("type")
                if mtype == "idle":
                    is_idle = True
                    idle_outcome = str(m.get("outcome") or "")
                elif mtype == "error":
                    err_msg = json.dumps(m.get("error") or m)
                elif mtype == "assistant":
                    if m.get("error"):
                        err_msg = json.dumps(m["error"])
                    if isinstance(m.get("tokens"), dict):
                        tokens = m["tokens"]
                    content = m.get("content") or []
                    if isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "text" and part.get("text"):
                                text_parts.append(part["text"])
                            elif part.get("type") == "reasoning" and part.get("text"):
                                reasoning_parts.append(part["text"])

            if err_msg:
                last_err = err_msg
                return None, "", tokens, err_msg

            if is_idle:
                if idle_outcome == "succeeded" or text_parts or reasoning_parts:
                    final_text = "\n".join(text_parts)
                    final_reasoning = "\n".join(reasoning_parts)
                    if not final_text and final_reasoning:
                        final_text = final_reasoning
                    return final_text, final_reasoning, tokens, "ok"
                return None, "", tokens, f"daemon idle outcome={idle_outcome}"

        return None, "", {}, f"daemon timeout after {DAEMON_TIMEOUT:.0f}s ({last_err})"
    except Exception as e:
        return None, "", {}, f"daemon exception: {e}"
    finally:
        if sid:
            try:
                _daemon_req(f"/api/session/{sid}", "DELETE", timeout=3.0)
            except Exception:
                pass


def run_via_cli(oc_model: str, prompt: str) -> tuple[str | None, str, dict, str]:
    """Failover via standalone CLI with proxy rotation + direct fallback."""
    proxies = [p for p in _load_proxy_candidates() if p.startswith("http")]
    random.shuffle(proxies)
    attempts = proxies[:5] + ["http://127.0.0.1:20988", ""]  # HTTP proxies + relay (SOCKS5+HTTP) + direct

    for proxy in attempts:
        temp_home = tempfile.mkdtemp(prefix="oc_cli_")
        env = os.environ.copy()
        env["HOME"] = temp_home
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
        env["no_proxy"] = "localhost,127.0.0.1,::1"
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy

        try:
            proc = subprocess.run(
                [OPENCODE_BIN, "run", "--standalone", "-m", oc_model, "--format", "json"],
                cwd=EMPTY_WORKDIR,
                env=env,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tokens: dict = {}
            err_msg = ""

            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                etype = ev.get("type")
                part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
                if etype == "text" and part.get("text"):
                    text_parts.append(part["text"])
                elif etype == "reasoning" and part.get("text"):
                    reasoning_parts.append(part["text"])
                elif etype == "step_finish" and isinstance(part.get("tokens"), dict):
                    tokens = part["tokens"]
                elif etype == "error":
                    err_msg = json.dumps(ev.get("error") or ev)

            combined_err = (err_msg + " " + proc.stderr).strip()
            if text_parts or reasoning_parts:
                final_text = "".join(text_parts)
                final_reasoning = "".join(reasoning_parts)
                if not final_text and final_reasoning:
                    final_text = final_reasoning
                return final_text, final_reasoning, tokens, f"cli:{'direct' if not proxy else 'proxy'}"

            if "FreeUsageLimitError" in combined_err or "429" in combined_err or "402" in combined_err or "407" in combined_err:
                _cool_proxy(proxy)
                log(f"CLI proxy limited ({'direct' if not proxy else proxy}): {combined_err[:120]}")
                continue
            if proc.returncode != 0:
                _cool_proxy(proxy, 180.0)
                log(f"CLI error rc={proc.returncode} ({'direct' if not proxy else proxy}): {combined_err[:120]}")
        except subprocess.TimeoutExpired:
            _cool_proxy(proxy, 300.0)
            log(f"CLI timeout after {CLI_TIMEOUT:.0f}s ({'direct' if not proxy else proxy})")
        except Exception as e:
            _cool_proxy(proxy, 180.0)
            log(f"CLI exception ({'direct' if not proxy else proxy}): {e}")
        finally:
            shutil.rmtree(temp_home, ignore_errors=True)

    return None, "", {}, "all opencode routes exhausted"


def run_exact_dahl_glm53(messages_or_prompt) -> tuple[str | None, str, dict, str]:
    """Exact-model failover ONLY for glm-5.3-flash -> dahl zai-org/GLM-5.3-Flash."""
    if not os.path.exists(DAHL_KEYS_FILE):
        return None, "", {}, "no dahl_keys.json"
    try:
        with open(DAHL_KEYS_FILE, "r", encoding="utf-8") as f:
            keys = json.load(f)
        if not isinstance(keys, list) or not keys:
            return None, "", {}, "empty dahl_keys.json"
    except Exception as e:
        return None, "", {}, f"read dahl_keys failed: {e}"

    sample_keys = random.sample(keys, min(3, len(keys)))
    msgs = messages_or_prompt if isinstance(messages_or_prompt, list) else [{"role": "user", "content": str(messages_or_prompt)}]
    payload = json.dumps({
        "model": "zai-org/GLM-5.3-Flash",
        "messages": msgs,
        "stream": False,
        "max_tokens": 4096,
    }).encode()

    for key in sample_keys:
        try:
            req = urllib.request.Request(
                "https://inference.dahl.global/v1/chat/completions",
                data=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                text = msg.get("content") or ""
                reasoning = msg.get("reasoning_content") or ""
                if text or reasoning:
                    return text, reasoning, data.get("usage") or {}, "dahl-exact"
        except Exception:
            continue
    return None, "", {}, "dahl exact fallback exhausted"


def _openai_usage(tokens: dict) -> dict:
    p = int(tokens.get("input") or tokens.get("prompt_tokens") or 0)
    c = int(tokens.get("output") or tokens.get("completion_tokens") or 0) + int(tokens.get("reasoning") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send_json(self, status: int, obj: dict) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _write_sse(self, event_name: str | None, obj: dict | str) -> None:
        lines = []
        if event_name:
            lines.append(f"event: {event_name}")
        payload = obj if isinstance(obj, str) else json.dumps(obj)
        lines.append(f"data: {payload}")
        lines.append("")
        lines.append("")
        chunk = ("\n".join(lines)).encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/"):
            daemon_ok = False
            try:
                st, _ = _daemon_req("/api/session", "GET", timeout=3.0)
                daemon_ok = st in (200, 404)
            except Exception:
                daemon_ok = False
            verified_cnt = 0
            vpath = PROXY_SOURCES[0]
            if os.path.exists(vpath):
                try:
                    verified_cnt = sum(1 for l in open(vpath, encoding="utf-8", errors="ignore") if l.strip() and not l.strip().startswith("#"))
                except Exception:
                    pass
            self._send_json(200, {
                "status": "ok",
                "daemon_url": DAEMON_URL,
                "daemon_alive": daemon_ok,
                "models": len(MODEL_MAP),
                "verified_proxies": verified_cnt,
            })
            return

        if path in ("/v1/models", "/models"):
            now = int(time.time())
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": k, "object": "model", "created": now, "owned_by": "opencode"}
                    for k in MODEL_MAP
                ],
            })
            return

        self._send_json(404, {"error": {"message": f"Not found: {path}", "type": "invalid_request_error"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v1/chat/completions", "/chat/completions", "/v1/messages", "/messages"):
            self._send_json(404, {"error": {"message": f"Unsupported path: {path}", "type": "invalid_request_error"}})
            return

        is_anthropic = path.endswith("/messages")
        length = int(self.headers.get("Content-Length") or "0")
        raw_body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(raw_body.decode("utf-8", "replace"))
        except Exception:
            self._send_json(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})
            return

        model_req = str(req.get("model") or "")
        model_key = resolve_model(model_req)
        if not model_key:
            self._send_json(404, {
                "error": {
                    "message": f"Model '{model_req}' is not available on OpenCode Unlimited Bridge",
                    "type": "model_not_found",
                    "code": "model_not_found",
                }
            })
            return

        oc_model = MODEL_MAP[model_key]
        is_stream = bool(req.get("stream"))
        prompt = extract_prompt(req, is_anthropic=is_anthropic)

        t0 = time.time()
        with SEM:
            text, reasoning, tokens, route_used = run_via_daemon(oc_model, prompt)
            if text is None:
                log(f"{model_key} daemon failed ({route_used}), failing over to CLI rotation")
                text, reasoning, tokens, route_used = run_via_cli(oc_model, prompt)
            if text is None and model_key == "glm-5.3-flash":
                log(f"{model_key} CLI failed ({route_used}), trying exact-model dahl zai-org/GLM-5.3-Flash")
                text, reasoning, tokens, route_used = run_exact_dahl_glm53(req.get("messages") or prompt)

        elapsed = time.time() - t0
        if text is None:
            log(f"FAIL {model_key} ({elapsed:.2f}s): {route_used}")
            self._send_json(429 if "FreeUsageLimitError" in route_used or "429" in route_used else 503, {
                "error": {
                    "message": f"OpenCode upstream temporarily unavailable for {model_key}: {route_used}",
                    "type": "upstream_unavailable",
                }
            })
            return

        log(f"OK {model_key} via {route_used} ({elapsed:.2f}s, {len(text)} chars)")
        usage = _openai_usage(tokens)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if is_anthropic:
            if is_stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                msg_id = f"msg_{uuid.uuid4().hex[:20]}"
                self._write_sse("message_start", {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model_req or model_key,
                        "stop_reason": None,
                        "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": 0},
                    },
                })
                self._write_sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                self._write_sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                self._write_sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                self._write_sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": usage["completion_tokens"]}})
                self._write_sse("message_stop", {"type": "message_stop"})
                return

            content_blocks = []
            if reasoning:
                content_blocks.append({"type": "thinking", "thinking": reasoning})
            content_blocks.append({"type": "text", "text": text})
            self._send_json(200, {
                "id": f"msg_{uuid.uuid4().hex[:20]}",
                "type": "message",
                "role": "assistant",
                "model": model_req or model_key,
                "content": content_blocks,
                "stop_reason": "end_turn",
                "usage": {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]},
            })
            return

        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_sse(None, {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_req or model_key,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            })
            if reasoning:
                self._write_sse(None, {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_req or model_key,
                    "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}, "finish_reason": None}],
                })
            # Stream in modest chunks so clients receive responsive output
            step = 128
            for i in range(0, max(1, len(text)), step):
                part = text[i:i + step]
                self._write_sse(None, {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_req or model_key,
                    "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                })
            self._write_sse(None, {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_req or model_key,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": usage,
            })
            self._write_sse(None, "[DONE]")
            return

        msg_obj: dict = {"role": "assistant", "content": text}
        if reasoning:
            msg_obj["reasoning_content"] = reasoning
        self._send_json(200, {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": model_req or model_key,
            "choices": [{"index": 0, "message": msg_obj, "finish_reason": "stop"}],
            "usage": usage,
        })


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    log(f"Starting OpenCode Unlimited Bridge on 127.0.0.1:{BRIDGE_PORT} ({len(MODEL_MAP)} models)")
    srv = BridgeServer(("127.0.0.1", BRIDGE_PORT), Handler)
    srv.serve_forever()
