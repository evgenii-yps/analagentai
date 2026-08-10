#!/usr/bin/env python3
"""Гео-тест доступности OKX (§4 ТЗ 6.5, блокирующий).

Проверяет, что с ЭТОГО сервера биржа OKX доступна и не заблокирована по гео:

1. REST: запрос публичного тикера. Коды ответа 451/403 (или сетевой отказ)
   трактуются как гео-блокировка / недоступность.
2. WebSocket: подписка на публичный канал ``tickers`` и ожидание реальных
   данных. Если данные по WebSocket не пришли — тест провален.

Скрипт написан ТОЛЬКО на стандартной библиотеке Python 3 (socket/ssl/urllib),
поэтому запускается на «голом» сервере до установки Docker и pip-пакетов.

Коды возврата:
    0 — OKX доступна (REST 200 + данные по WebSocket получены);
    1 — провал (печатаются коды ответов и диагностика).

Запуск: ``python3 scripts/geo_check.py``.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.request

# --- Параметры (можно переопределить переменными окружения) ---
REST_URL = os.environ.get(
    "GEO_OKX_REST_URL",
    "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT",
)
WS_HOST = os.environ.get("GEO_OKX_WS_HOST", "ws.okx.com")
WS_PORT = int(os.environ.get("GEO_OKX_WS_PORT", "8443"))
WS_PATH = os.environ.get("GEO_OKX_WS_PATH", "/ws/v5/public")
WS_INST = os.environ.get("GEO_OKX_WS_INST", "BTC-USDT")
REST_TIMEOUT = float(os.environ.get("GEO_REST_TIMEOUT", "15"))
WS_TIMEOUT = float(os.environ.get("GEO_WS_TIMEOUT", "20"))

# Коды, которые однозначно указывают на гео-/сетевую блокировку.
BLOCKING_STATUSES = {403, 451}


def _log(msg: str) -> None:
    """Печатает диагностическую строку (на русском) в stdout."""
    print(msg, flush=True)


def check_rest() -> bool:
    """REST-проверка OKX. True — если получен корректный ответ 200 с данными."""
    _log(f"[REST] Запрос к OKX: {REST_URL}")
    req = urllib.request.Request(REST_URL, headers={"User-Agent": "agent-trade-geocheck/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=REST_TIMEOUT) as resp:
            status = resp.getcode()
            body = resp.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in BLOCKING_STATUSES:
            _log(f"[REST] ПРОВАЛ: HTTP {status} — OKX недоступна из этой локации (гео-блокировка).")
        else:
            _log(f"[REST] ПРОВАЛ: HTTP {status} — неожиданный код ответа от OKX.")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(f"[REST] ПРОВАЛ: сетевая ошибка при обращении к OKX: {exc}")
        return False

    if status != 200:
        _log(f"[REST] ПРОВАЛ: HTTP {status} (ожидался 200).")
        return False

    # OKX возвращает {"code":"0","data":[...]} при успехе.
    try:
        parsed = json.loads(body)
    except ValueError:
        _log(f"[REST] ПРОВАЛ: ответ не является JSON: {body[:200]}")
        return False

    if parsed.get("code") == "0" and parsed.get("data"):
        _log("[REST] OK: HTTP 200, данные тикера получены.")
        return True

    _log(f"[REST] ПРОВАЛ: OKX вернула бизнес-ошибку: {body[:200]}")
    return False


def _ws_encode_text_frame(payload: bytes) -> bytes:
    """Кодирует текстовый WebSocket-кадр (маскированный, как требуется клиенту)."""
    fin_opcode = 0x81  # FIN=1, opcode=0x1 (text)
    mask_key = os.urandom(4)
    length = len(payload)
    header = bytearray([fin_opcode])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask_key
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def _ws_read_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """Читает один WebSocket-кадр от сервера. Возвращает (opcode, payload) или None."""

    def _recv_exactly(n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    header = _recv_exactly(2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        ext = _recv_exactly(2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = _recv_exactly(8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
    mask_key = b""
    if masked:
        mask_key = _recv_exactly(4)
        if mask_key is None:
            return None
    payload = _recv_exactly(length) if length else b""
    if payload is None:
        return None
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def check_websocket() -> bool:
    """WebSocket-проверка OKX. True — если пришёл кадр с реальными данными канала."""
    _log(f"[WS] Подключение к wss://{WS_HOST}:{WS_PORT}{WS_PATH}")
    ctx = ssl.create_default_context()
    ca_file = os.environ.get("SSL_CERT_FILE")
    if ca_file:
        try:
            ctx.load_verify_locations(cafile=ca_file)
        except OSError:
            pass

    raw_sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    try:
        raw_sock = socket.create_connection((WS_HOST, WS_PORT), timeout=WS_TIMEOUT)
        tls_sock = ctx.wrap_socket(raw_sock, server_hostname=WS_HOST)
        tls_sock.settimeout(WS_TIMEOUT)

        # HTTP Upgrade handshake.
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {WS_HOST}:{WS_PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        tls_sock.sendall(handshake.encode("ascii"))

        # Читаем заголовки ответа до пустой строки.
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = tls_sock.recv(1024)
            if not chunk:
                break
            resp += chunk
            if len(resp) > 65536:
                break
        status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if "101" not in status_line:
            _log(f"[WS] ПРОВАЛ: рукопожатие отклонено сервером: {status_line}")
            return False
        _log("[WS] Рукопожатие успешно (HTTP 101). Подписка на канал tickers…")

        # Подписка на публичный канал tickers.
        sub = json.dumps(
            {"op": "subscribe", "args": [{"channel": "tickers", "instId": WS_INST}]}
        ).encode("utf-8")
        tls_sock.sendall(_ws_encode_text_frame(sub))

        # Ждём кадр с реальными данными (поле "data" непустое).
        while True:
            frame = _ws_read_frame(tls_sock)
            if frame is None:
                _log("[WS] ПРОВАЛ: соединение закрыто до получения данных.")
                return False
            opcode, payload = frame
            if opcode == 0x8:  # close
                _log("[WS] ПРОВАЛ: сервер закрыл соединение (close frame).")
                return False
            if opcode == 0x9:  # ping -> pong
                continue
            if opcode not in (0x1, 0x2):  # интересуют text/binary
                continue
            text = payload.decode("utf-8", "replace")
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if msg.get("event") == "error":
                _log(f"[WS] ПРОВАЛ: сервер вернул ошибку подписки: {text[:200]}")
                return False
            if msg.get("data"):
                _log("[WS] OK: получены реальные данные по WebSocket.")
                return True
            # event == subscribe (ack) — ждём следующий кадр с данными.
    except (ssl.SSLError, TimeoutError, OSError) as exc:
        _log(f"[WS] ПРОВАЛ: ошибка WebSocket-соединения с OKX: {exc}")
        return False
    finally:
        try:
            if tls_sock is not None:
                tls_sock.close()
            elif raw_sock is not None:
                raw_sock.close()
        except OSError:
            pass


def main() -> int:
    """Точка входа: печатает итог и возвращает exit code (0 — успех)."""
    _log("=== Гео-тест OKX (блокирующий шаг развёртывания) ===")
    rest_ok = check_rest()
    ws_ok = check_websocket()

    _log("")
    _log("--- Итог гео-теста ---")
    _log(f"REST OKX:      {'OK' if rest_ok else 'ПРОВАЛ'}")
    _log(f"WebSocket OKX: {'OK' if ws_ok else 'ПРОВАЛ'}")

    if rest_ok and ws_ok:
        _log("РЕЗУЛЬТАТ: OKX доступна из этой локации. Можно продолжать развёртывание.")
        return 0

    _log("РЕЗУЛЬТАТ: OKX недоступна из этой локации — развёртывание НЕВОЗМОЖНО.")
    _log("Что делать: сменить локацию сервера (регион дата-центра) на разрешённую для OKX")
    _log("и запустить установщик заново.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
