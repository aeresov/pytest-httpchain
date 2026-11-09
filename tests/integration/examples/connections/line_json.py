"""The reference adapter of docs/usage/connections.md: newline-delimited
JSON over TCP, for the examples conftest's ``line_json_server``. Keep the
two in sync."""

import json
import select
import socket
import threading
import time


class LineJsonAdapter:
    """Newline-delimited JSON over TCP."""

    def __init__(self, host: str, port: int):
        self.address = (host, int(port))
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.seq = 0
        self.write_lock = threading.Lock()

    def connect(self, timeout: float) -> dict | None:
        self.sock = socket.create_connection(self.address, timeout=timeout)
        self.sock.settimeout(None)  # blocking from here on: receive() waits with select
        return self.receive(timeout)  # the greeting is the handshake result

    def send(self, message: dict) -> dict:
        assert self.sock is not None
        with self.write_lock:
            self.seq += 1
            frame = {"seq": self.seq, **message}  # the server's reply carries it back as reply_to
            self.sock.sendall(json.dumps(frame).encode() + b"\n")
            return {"seq": self.seq}  # the stage reads it as `sent`

    def receive(self, timeout: float) -> dict | None:
        assert self.sock is not None
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed by peer")  # a clean end
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes a receive() in progress
            except OSError:
                pass
            self.sock.close()

    def debug_info(self) -> str:
        return f"unread bytes: {self.buffer[:200].hex()}"
