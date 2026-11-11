"""Just enough Nostr for Hive: BIP-340 keys and signatures, NIP-01 events, NIP-13 proof of work, and a small
multi-relay client. Standard library plus `websockets` (already a Hermes dependency).

The BIP-340 code follows the reference implementation in the BIP (public domain), with Jacobian coordinates so a
signature takes milliseconds rather than seconds in pure Python.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from typing import Iterable, Optional

# ── secp256k1 / BIP-340 ─────────────────────────────────────────────────────────────────────────────────────────
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _jac_double(p):
    x, y, z = p
    if y == 0:
        return (0, 0, 0)
    ysq = y * y % P
    s = 4 * x * ysq % P
    m = 3 * x * x % P
    nx = (m * m - 2 * s) % P
    ny = (m * (s - nx) - 8 * ysq * ysq) % P
    return (nx, ny, 2 * y * z % P)


def _jac_add(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1s, z2s = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2s % P, x2 * z1s % P
    s1, s2 = y1 * z2s * z2 % P, y2 * z1s * z1 % P
    if u1 == u2:
        return _jac_double(p) if s1 == s2 else (0, 0, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    h2 = h * h % P
    h3 = h * h2 % P
    u1h2 = u1 * h2 % P
    nx = (r * r - h3 - 2 * u1h2) % P
    ny = (r * (u1h2 - nx) - s1 * h3) % P
    return (nx, ny, h * z1 * z2 % P)


def _to_affine(p):
    if p[2] == 0:
        return None
    zi = pow(p[2], P - 2, P)
    zi2 = zi * zi % P
    return (p[0] * zi2 % P, p[1] * zi2 * zi % P)


def _mul(point, k: int):
    acc, add = (0, 0, 0), (point[0], point[1], 1)
    while k:
        if k & 1:
            acc = _jac_add(acc, add)
        add = _jac_double(add)
        k >>= 1
    return _to_affine(acc)


def _add_affine(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return _to_affine(_jac_add((a[0], a[1], 1), (b[0], b[1], 1)))


def _tagged(tag: str, msg: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + msg).digest()


def _lift_x(x: int):
    if x >= P:
        return None
    ysq = (pow(x, 3, P) + 7) % P
    y = pow(ysq, (P + 1) // 4, P)
    if y * y % P != ysq:
        return None
    return (x, y if y % 2 == 0 else P - y)


def pubkey_of(seckey: bytes) -> bytes:
    d = int.from_bytes(seckey, "big")
    if not 0 < d < N:
        raise ValueError("secret key out of range")
    return _mul(G, d)[0].to_bytes(32, "big")


def schnorr_sign(msg32: bytes, seckey: bytes, aux: Optional[bytes] = None) -> bytes:
    d0 = int.from_bytes(seckey, "big")
    if not 0 < d0 < N:
        raise ValueError("secret key out of range")
    pt = _mul(G, d0)
    d = d0 if pt[1] % 2 == 0 else N - d0
    aux = aux if aux is not None else secrets.token_bytes(32)
    t = (d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t + pt[0].to_bytes(32, "big") + msg32), "big") % N
    if k0 == 0:
        raise ValueError("bad nonce")
    r = _mul(G, k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    e = int.from_bytes(_tagged("BIP0340/challenge", r[0].to_bytes(32, "big") + pt[0].to_bytes(32, "big") + msg32),
                       "big") % N
    return r[0].to_bytes(32, "big") + ((k + e * d) % N).to_bytes(32, "big")


def schnorr_verify(msg32: bytes, pubkey: bytes, sig: bytes) -> bool:
    if len(pubkey) != 32 or len(sig) != 64:
        return False
    pt = _lift_x(int.from_bytes(pubkey, "big"))
    r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
    if pt is None or r >= P or s >= N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pubkey + msg32), "big") % N
    rr = _add_affine(_mul(G, s), _mul(pt, N - e))
    return rr is not None and rr[1] % 2 == 0 and rr[0] == r


# ── NIP-01 events, NIP-13 proof of work ────────────────────────────────────────────────────────────────────────

def event_id(ev: dict) -> str:
    data = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]],
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def leading_zero_bits(hex_id: str) -> int:
    n = int(hex_id, 16)
    return 256 - n.bit_length() if n else 256


def make_event(seckey: bytes, kind: int, tags: list, content: str, pow_bits: int = 0,
               created_at: Optional[int] = None, max_tries: int = 5_000_000) -> dict:
    ev = {"pubkey": pubkey_of(seckey).hex(), "created_at": int(created_at or time.time()), "kind": kind,
          "tags": [list(t) for t in tags], "content": content}
    if pow_bits > 0:
        ev["tags"].append(["nonce", "0", str(pow_bits)])
        for n in range(max_tries):
            ev["tags"][-1][1] = str(n)
            eid = event_id(ev)
            if leading_zero_bits(eid) >= pow_bits:
                break
        else:
            raise RuntimeError("proof of work not found")
    ev["id"] = event_id(ev)
    ev["sig"] = schnorr_sign(bytes.fromhex(ev["id"]), seckey).hex()
    return ev


def verify_event(ev: dict, min_pow: int = 0) -> bool:
    try:
        if event_id(ev) != ev["id"]:
            return False
        if min_pow:
            committed = next((int(t[2]) for t in ev["tags"] if t and t[0] == "nonce" and len(t) > 2), 0)
            if committed < min_pow or leading_zero_bits(ev["id"]) < min_pow:
                return False
        return schnorr_verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"]))
    except Exception:  # noqa: BLE001 - malformed input from the network is simply invalid
        return False


def load_or_create_key(path) -> bytes:
    """The install's random identity. Not the user's: nothing ties it to a name, email or account."""
    from pathlib import Path
    p = Path(path)
    if p.exists():
        raw = bytes.fromhex(p.read_text().strip())
        if len(raw) == 32:
            return raw
    key = secrets.token_bytes(32)
    while not 0 < int.from_bytes(key, "big") < N:
        key = secrets.token_bytes(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    os.replace(tmp, p)
    return key


# ── relays ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def _publish_one(url: str, ev: dict, timeout: float) -> tuple[str, bool, str]:
    import websockets
    try:
        async with websockets.connect(url, open_timeout=timeout, close_timeout=1, max_size=2 ** 20) as ws:
            await ws.send(json.dumps(["EVENT", ev]))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic())))
                if msg and msg[0] == "OK" and msg[1] == ev["id"]:
                    return url, bool(msg[2]), str(msg[3] if len(msg) > 3 else "")
            return url, False, "no answer"
    except Exception as exc:  # noqa: BLE001
        return url, False, f"{type(exc).__name__}: {str(exc)[:120]}"


async def _query_one(url: str, flt: dict, timeout: float) -> tuple[str, list, str]:
    import websockets
    got: list = []
    try:
        async with websockets.connect(url, open_timeout=timeout, close_timeout=1, max_size=2 ** 22) as ws:
            sub = secrets.token_hex(4)
            await ws.send(json.dumps(["REQ", sub, flt]))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic())))
                if msg[0] == "EVENT" and msg[1] == sub:
                    got.append(msg[2])
                elif msg[0] in ("EOSE", "CLOSED") and msg[1] == sub:
                    break
            try:
                await ws.send(json.dumps(["CLOSE", sub]))
            except Exception:  # noqa: BLE001
                pass
            return url, got, ""
    except Exception as exc:  # noqa: BLE001
        return url, got, f"{type(exc).__name__}: {str(exc)[:120]}"


def _run(make_coro, timeout: float):
    """Run make_coro() on a private loop in a helper thread, so callers inside another event loop are safe."""
    import threading
    box: dict = {}

    def target():
        try:
            async def main():
                return await asyncio.wait_for(make_coro(), timeout)
            box["v"] = asyncio.run(main())
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout + 1)
    if "v" in box:
        return box["v"]
    raise TimeoutError(str(box.get("e") or "relay call timed out"))


def publish(relays: Iterable[str], ev: dict, timeout: float = 6.0) -> list[tuple[str, bool, str]]:
    relays = list(relays)
    if not relays:
        return []
    async def go():
        return await asyncio.gather(*(_publish_one(r, ev, timeout) for r in relays))
    try:
        return _run(go, timeout + 2)
    except TimeoutError as exc:
        return [(r, False, str(exc)) for r in relays]


def query(relays: Iterable[str], flt: dict, timeout: float = 4.0) -> tuple[list, list[tuple[str, str]]]:
    """Events from all relays (deduplicated by id), plus (relay, error) for relays that failed."""
    relays = list(relays)
    if not relays:
        return [], []
    try:
        async def go():
            return await asyncio.gather(*(_query_one(r, flt, timeout) for r in relays))
        results = _run(go, timeout + 2)
    except TimeoutError as exc:
        return [], [(r, str(exc)) for r in relays]
    seen, events, errors = set(), [], []
    for url, got, err in results:
        if err:
            errors.append((url, err))
        for ev in got:
            if isinstance(ev, dict) and ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
    return events, errors
