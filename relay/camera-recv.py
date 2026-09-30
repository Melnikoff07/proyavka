#!/usr/bin/env python3
"""Приёмник кадров с камеры (приложение «Проявка» для Sony) для бота «Проявка».

PUT /camera/upload/<имя>  (заголовок X-Token) — тело пишется во временный файл,
проверяется (размер, JPEG целиком) и атомарно переносится в UPLOAD_DIR,
где его уже ждёт inotifywait бота (событие moved_to).
Ответ: {"ok": true, "sha1": "...", "size": N} — камера сверяет SHA-1 со своим.
GET /camera/ping — проверка связи и токена.

Слушает только 127.0.0.1, снаружи доступен через nginx.
"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

TOKEN = os.environ.get("CAMERA_TOKEN", "")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/srv/camera/upload")
TMP_DIR = os.path.join(UPLOAD_DIR, ".incoming")   # та же ФС, чтобы rename был атомарным; бот смотрит только файлы верхнего уровня
PORT = int(os.environ.get("PORT", "8089"))
HOST = os.environ.get("HOST", "127.0.0.1")
MAX_SIZE = 80 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}\.(jpg|jpeg|JPG|JPEG)$")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class H(BaseHTTPRequestHandler):
    server_version = "camera-recv"
    protocol_version = "HTTP/1.1"

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def authed(self):
        got = self.headers.get("X-Token", "")
        if TOKEN and hmac.compare_digest(got.encode(), TOKEN.encode()):
            return True
        self.reply(401, {"ok": False, "error": "bad token"})
        return False

    def do_GET(self):
        if self.path.split("?")[0] != "/camera/ping":
            return self.reply(404, {"ok": False})
        if self.authed():
            self.reply(200, {"ok": True})

    def do_PUT(self):
        prefix = "/camera/upload/"
        if not self.path.startswith(prefix):
            return self.reply(404, {"ok": False})
        if not self.authed():
            return
        name = unquote(self.path[len(prefix):].split("?")[0])
        if not NAME_RE.match(name):
            return self.reply(400, {"ok": False, "error": "bad name"})
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.reply(411, {"ok": False, "error": "no length"})
        if size <= 1024 or size > MAX_SIZE:
            return self.reply(413, {"ok": False, "error": "bad size"})

        os.makedirs(TMP_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=TMP_DIR, suffix=".part")
        h = hashlib.sha1()
        left = size
        tail = b""
        try:
            with os.fdopen(fd, "wb") as f:
                while left:
                    chunk = self.rfile.read(min(left, 256 * 1024))
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    tail = (tail + chunk)[-65536:]
                    left -= len(chunk)
                f.flush()
                os.fsync(f.fileno())
            if left:
                raise ValueError("connection dropped, %d bytes missing" % left)
            # a6300 добивает JPEG нулями после FFD9 (~12 КБ), их отбрасываем
            if b"\xff\xd9" not in tail.rstrip(b"\x00")[-64:]:
                raise ValueError("not a complete JPEG")
            os.chmod(tmp, 0o664)   # как у vsftpd (local_umask=002): группа photos, из неё бот забирает кадры
            os.replace(tmp, os.path.join(UPLOAD_DIR, name))
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            log("reject", name, e)
            return self.reply(400, {"ok": False, "error": str(e)})
        digest = h.hexdigest()
        log("saved", name, size, digest)
        self.reply(200, {"ok": True, "sha1": digest, "size": size})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("CAMERA_TOKEN is not set")
    log("camera-recv on %s:%d -> %s" % (HOST, PORT, UPLOAD_DIR))
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
