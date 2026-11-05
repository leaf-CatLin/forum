# -*- coding: utf-8 -*-
"""ALTCHA v2 客户端求解（与 Web / 安卓端同源）。

服务端 ``/ov2/api/captcha/challenge`` 下发 ``{parameters:{algorithm,nonce,salt,
cost,keyLength,keyPrefix,expiresAt,signature}}``；客户端从 0 开始扫描 counter，
用 PBKDF2-HMAC-SHA256 派生 key，直到其十六进制以 ``keyPrefix`` 开头，然后把
``{challenge, solution}`` 做 Base64 交给 ``POST /ov2/api/captcha/verify`` 换 grant。

服务端生成 counter 时取 ``[cost, 2*cost)``，这里优先扫该区间，未命中再回退
``[0, cost)``（与安卓实现一致）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_COST = 5000
DEFAULT_KEY_LENGTH = 32
MAX_COST = 20_000
MAX_KEY_LENGTH = 64
MAX_PREFIX_HEX = 128
MAX_HEX_CHARS = 128
MAX_WORKERS = 4


def _hex_decode(value: str) -> bytes | None:
    text = str(value or "").strip()
    if len(text) % 2 != 0:
        return None
    try:
        return bytes.fromhex(text)
    except ValueError:
        return None


def _prefix_matches(prefix_hex: str, key: bytes) -> bool:
    prefix = str(prefix_hex or "").strip().lower()
    if not prefix:
        return False
    whole = len(prefix) - (len(prefix) % 2)
    head = bytes.fromhex(prefix[:whole]) if whole else b""
    if len(head) > len(key):
        return False
    if key[:len(head)] != head:
        return False
    if len(prefix) % 2 == 0:
        return True
    if len(head) >= len(key):
        return False
    nibble = int(prefix[-1], 16)
    return (key[len(head)] & 0xF0) == (nibble << 4)


def _scan(nonce: bytes, salt: bytes, cost: int, key_length: int,
          prefix: str, start: int, stop: int, step: int,
          deadline: float, stop_event: threading.Event) -> tuple[int, bytes] | None:
    counter = start
    while counter < stop:
        if stop_event.is_set():
            return None
        key = hashlib.pbkdf2_hmac(
            "sha256", nonce + counter.to_bytes(4, "big"), salt, cost, key_length)
        if _prefix_matches(prefix, key):
            return counter, key
        counter += step
        if counter % 256 < step and time.monotonic() >= deadline:
            return None
    return None


def solve(challenge: dict, *, workers: int = MAX_WORKERS,
          deadline_s: float = 60.0) -> dict | None:
    """求解 challenge；失败/超时返回 ``None``。"""
    if not isinstance(challenge, dict):
        return None
    params = challenge.get("parameters")
    if not isinstance(params, dict):
        return None
    algorithm = str(params.get("algorithm") or "").upper()
    if not algorithm.startswith("PBKDF2/SHA-256"):
        return None
    nonce = _hex_decode(params.get("nonce"))
    salt = _hex_decode(params.get("salt"))
    if not nonce or not salt:
        return None
    if len(nonce) > MAX_HEX_CHARS // 2 or len(salt) > MAX_HEX_CHARS // 2:
        return None
    try:
        cost = int(params.get("cost") or DEFAULT_COST)
        key_length = int(params.get("keyLength") or DEFAULT_KEY_LENGTH)
    except (TypeError, ValueError):
        return None
    if not 1 <= cost <= MAX_COST or not 1 <= key_length <= MAX_KEY_LENGTH:
        return None
    prefix = str(params.get("keyPrefix") or "").strip().lower()
    if not prefix or len(prefix) > MAX_PREFIX_HEX or len(prefix) > key_length * 2:
        return None
    try:
        int(prefix, 16)
    except ValueError:
        return None

    started = time.monotonic()
    begun_at = int(time.time() * 1000)
    stop_event = threading.Event()
    n_workers = max(1, min(int(workers), MAX_WORKERS))
    found: list[tuple[int, bytes]] = []

    def _run_span(start: int, stop: int) -> None:
        chunk = (stop - start) // n_workers + 1
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = [
                pool.submit(_scan, nonce, salt, cost, key_length, prefix,
                            start + i * chunk, min(stop, start + (i + 1) * chunk),
                            1, started + deadline_s, stop_event)
                for i in range(n_workers)
            ]
            for future in futures:
                outcome = future.result()
                if outcome is not None and not found:
                    found.append(outcome)
                    stop_event.set()

    # 1) 服务端生成区间；2) 回退低区间
    _run_span(cost, min(cost * 2, 2 ** 31 - 1))
    if not found:
        stop_event.clear()
        _run_span(0, cost)
    if not found:
        return None

    counter, key = found[0]
    elapsed = int(time.monotonic() * 1000) - begun_at
    return {
        "counter": counter,
        "derivedKey": key.hex(),
        "time": max(0, elapsed),
    }


def solve_payload(challenge: dict, **kwargs) -> str:
    """求解并返回服务端可校验的 Base64 payload；失败返回空串。"""
    solution = solve(challenge, **kwargs)
    if not solution:
        return ""
    body = {"challenge": challenge, "solution": solution}
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")
