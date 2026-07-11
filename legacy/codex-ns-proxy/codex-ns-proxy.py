#!/usr/bin/env python3
"""
Codex Namespace Proxy — flattens type:namespace tools into type:function tools
and splits function_call names back into name + namespace on the response side.
"""

import json
import ssl
import http.server
import http.client
import socketserver
import sys
import os
import threading
import traceback
from urllib.parse import urlparse

LISTEN_HOST = os.environ.get("NS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("NS_PROXY_PORT", "18999"))
UPSTREAM_URL = os.environ.get(
    "NS_PROXY_UPSTREAM",
    "https://e35274.compute.systalyze.com/mga/glm-52-fp8--base-m-m4tvs/v1",
)
DELIMITER = "__"
DEBUG = True

_up = urlparse(UPSTREAM_URL)
UP_HOST = _up.hostname
UP_PORT = _up.port or (443 if _up.scheme == "https" else 80)
UP_PATH = _up.path.rstrip("/")
# Strip trailing /v1 — Codex's request path already includes it
if UP_PATH.endswith("/v1"):
    UP_PATH = UP_PATH[:-3]
UP_TLS = _up.scheme == "https"
SSL_CTX = ssl.create_default_context()


def log(msg):
    sys.stderr.write(f"[ns-proxy] {msg}\n")
    sys.stderr.flush()


def flatten_request(data):
    namespaces = set()
    tools = data.get("tools")
    if isinstance(tools, list):
        flat = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace":
                ns_name = tool.get("name", "")
                if ns_name:
                    namespaces.add(ns_name)
                desc = tool.get("description", "")
                for sub in tool.get("tools", []):
                    if isinstance(sub, dict) and sub.get("type") == "function":
                        sub = dict(sub)
                        orig = sub.get("name", "")
                        sub["name"] = f"{ns_name}{DELIMITER}{orig}"
                        if desc and sub.get("description"):
                            sub["description"] = f"[{ns_name}] {desc}\n\n{sub['description']}"
                        elif desc:
                            sub["description"] = f"[{ns_name}] {desc}"
                        flat.append(sub)
                    else:
                        flat.append(sub)
            else:
                flat.append(tool)
        data["tools"] = flat

    inp = data.get("input")
    if isinstance(inp, list):
        data["input"] = [_flatten_input_item(item) for item in inp]
    return data, namespaces


def _flatten_input_item(item):
    if not isinstance(item, dict):
        return item
    itype = item.get("type", "")
    if itype in ("function_call", "custom_tool_call") and item.get("namespace"):
        ns = item.pop("namespace")
        item["name"] = f"{ns}{DELIMITER}{item.get('name', '')}"
    for k, v in list(item.items()):
        if isinstance(v, list):
            item[k] = [_flatten_input_item(x) for x in v]
    return item


def transform_response(data):
    if not isinstance(data, dict):
        return data
    item = data.get("item")
    if isinstance(item, dict):
        _split_call_name(item)
    resp = data.get("response")
    if isinstance(resp, dict):
        out = resp.get("output")
        if isinstance(out, list):
            for it in out:
                if isinstance(it, dict):
                    _split_call_name(it)
    out = data.get("output")
    if isinstance(out, list):
        for it in out:
            if isinstance(it, dict):
                _split_call_name(it)
    return data


def _split_call_name(item):
    if not isinstance(item, dict):
        return
    if item.get("type") in ("function_call", "custom_tool_call"):
        name = item.get("name", "")
        if DELIMITER in name:
            parts = name.split(DELIMITER, 1)
            item["name"] = parts[1]
            item["namespace"] = parts[0]


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def handle(self):
        log(f"CONNECTION from {self.client_address}")
        super().handle()
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        try:
            self._handle_post()
        except Exception as e:
            log(f"ERROR in do_POST: {e}")
            log(traceback.format_exc())
            try:
                self.send_error(500, str(e))
            except:
                pass

    def _handle_post(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        log(f"POST {self.path} | body={len(body)} bytes")

        try:
            data = json.loads(body)
            data, namespaces = flatten_request(data)
            modified = json.dumps(data).encode()
            log(f"  flattened {len(namespaces)} namespaces: {sorted(namespaces)}")
            log(f"  tools count: {len(data.get('tools', []))}")
        except (json.JSONDecodeError, TypeError) as e:
            log(f"  JSON parse error: {e}, passing through")
            modified = body

        up_path = UP_PATH + self.path
        fwd_headers = {}
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "content-length", "transfer-encoding",
                       "accept-encoding", "connection"):
                continue
            fwd_headers[k] = v
        fwd_headers["Host"] = UP_HOST
        fwd_headers["Content-Length"] = str(len(modified))
        fwd_headers["Accept-Encoding"] = "identity"
        fwd_headers["Connection"] = "close"

        log(f"  connecting to {UP_HOST}:{UP_PORT} path={up_path}")
        if UP_TLS:
            conn = http.client.HTTPSConnection(UP_HOST, UP_PORT, context=SSL_CTX)
        else:
            conn = http.client.HTTPConnection(UP_HOST, UP_PORT)
        conn.request("POST", up_path, body=modified, headers=fwd_headers)
        resp = conn.getresponse()

        ct = resp.getheader("Content-Type", "")
        is_sse = "text/event-stream" in ct
        log(f"  upstream response: status={resp.status} content_type={ct} sse={is_sse}")

        if is_sse:
            self._stream_sse(resp)
        else:
            self._forward_plain(resp)

        conn.close()
        log(f"  request complete")

    def _stream_sse(self, resp):
        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        line_count = 0
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    payload = line[6:].strip()
                    if payload and payload != b"[DONE]":
                        try:
                            obj = json.loads(payload)
                            obj = transform_response(obj)
                            line = b"data: " + json.dumps(obj).encode() + b"\n"
                        except (json.JSONDecodeError, TypeError):
                            pass
                self.wfile.write(line)
                self.wfile.flush()
                line_count += 1
            log(f"  streamed {line_count} lines")
        except (BrokenPipeError, ConnectionResetError) as e:
            log(f"  client disconnected: {e}")

    def _forward_plain(self, resp):
        body = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        log(f"  forwarded {len(body)} bytes")

    def log_message(self, *args):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    log(f"Listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    log(f"Upstream: {UPSTREAM_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
