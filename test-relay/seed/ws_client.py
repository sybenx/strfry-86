"""Minimal RFC 6455 WebSocket client, stdlib only.

Just enough to open one connection to a strfry relay, send a handful of
`["EVENT", ...]` messages, and read back the matching `["OK", ...]` replies.
Not a general WS client: no fragmentation, no ping/pong handling beyond
draining them, one connection per call. This is the one place the seeder
needs a real network client — publishing over the wire is what makes
plugin86 actually run, which `strfry import` (bypasses the write policy
entirely) cannot exercise. See test-relay/README.md.
"""

import base64
import hashlib
import json
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _handshake(sock, host, port, path="/"):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed during handshake")
        resp += chunk
    header, _, rest = resp.partition(b"\r\n\r\n")
    if b"101" not in header.split(b"\r\n", 1)[0]:
        raise ConnectionError(f"handshake rejected: {header!r}")
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if expected.encode() not in header:
        raise ConnectionError("handshake accept-key mismatch")
    return rest  # any payload bytes already read past the header


def _send_text_frame(sock, text):
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    header = bytearray()
    header.append(0x80 | 0x1)  # FIN + text opcode
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    sock.sendall(bytes(header) + mask + masked)


def _recv_exact(sock, n, buf):
    while len(buf) < n:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("socket closed mid-frame")
        buf += chunk
    data, rest = buf[:n], buf[n:]
    return data, rest


def _recv_frame(sock, buf):
    """Return (opcode, payload, remaining_buf). Server frames are unmasked."""
    head, buf = _recv_exact(sock, 2, buf)
    opcode = head[0] & 0x0F
    length = head[1] & 0x7F
    if length == 126:
        ext, buf = _recv_exact(sock, 2, buf)
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext, buf = _recv_exact(sock, 8, buf)
        length = struct.unpack(">Q", ext)[0]
    payload, buf = _recv_exact(sock, length, buf)
    return opcode, payload, buf


def publish_events(host, port, events, timeout=15):
    """Open one connection, publish each event in order, collect the OK
    reply for each. Returns a list of (event_id, ok, message)."""
    results = []
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        buf = _handshake(sock, host, port)
        for ev in events:
            _send_text_frame(sock, json.dumps(["EVENT", ev]))
            ok = None
            msg = ""
            while ok is None:
                opcode, payload, buf = _recv_frame(sock, buf)
                if opcode == 0x8:  # close
                    raise ConnectionError("relay closed connection")
                if opcode != 0x1:  # ignore ping/binary/etc
                    continue
                try:
                    reply = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(reply, list) and reply[:1] == ["OK"] and len(reply) >= 3 and reply[1] == ev["id"]:
                    ok = bool(reply[2])
                    msg = reply[3] if len(reply) >= 4 else ""
            results.append((ev["id"], ok, msg))
    finally:
        sock.close()
    return results
