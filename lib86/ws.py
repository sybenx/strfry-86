"""Minimal stdlib websocket client, enough to talk to a Nostr relay."""

import base64
import json
import os
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit

MAX_FRAME = 16 * 1024 * 1024


class WSClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url, timeout=15):
        parts = urlsplit(url)
        if parts.scheme not in ("ws", "wss"):
            raise ValueError("relay url must be ws:// or wss://")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        raw = socket.create_connection((host, port), timeout=timeout)
        if parts.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if parts.port is None else f"{host}:{port}"
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\nUser-Agent: eighty-six-sidecar\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"websocket upgrade refused: {status.decode(errors='replace')}")

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSClosed("connection closed during handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise ConnectionError("handshake response too large")
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WSClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send(self, obj):
        self._send_frame(0x1, json.dumps(obj, separators=(",", ":")).encode())

    def recv(self, timeout=None):
        """Next relay message as parsed JSON. socket.timeout if none arrives in time."""
        self.sock.settimeout(timeout)
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            # A timeout mid-frame would drop the header already read.
            self.sock.settimeout(30)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            if n > MAX_FRAME:
                raise ConnectionError("frame too large")
            mask = self._read_exact(4) if b2 & 0x80 else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WSClosed("relay closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if len(message) > MAX_FRAME:
                raise ConnectionError("message too large")
            if fin:
                return json.loads(message.decode("utf-8"))

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def query(url, filters, timeout=15):
    """One REQ, collected until EOSE (or timeout). Returns the events."""
    ws = WebSocket(url, timeout=timeout)
    try:
        ws.send(["REQ", "q", *filters])
        events = []
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                msg = ws.recv(timeout=left)
            except socket.timeout:
                break
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                events.append(msg[2])
            elif msg[0] in ("EOSE", "CLOSED"):
                break
        return events
    finally:
        ws.close()
