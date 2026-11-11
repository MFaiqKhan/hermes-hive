"""A tiny in-memory Nostr relay for tests and demos (NIP-01 EVENT/REQ/CLOSE, addressable kinds 30000-39999).

    python mock_relay.py 7447        # serves ws://127.0.0.1:7447 until killed
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading

import websockets


class Relay:
    def __init__(self):
        self.events: dict[str, dict] = {}

    def _key(self, ev: dict):
        if 30000 <= ev["kind"] < 40000:
            d = next((t[1] for t in ev["tags"] if t and t[0] == "d"), "")
            return ("addr", ev["pubkey"], ev["kind"], d)
        return ("id", ev["id"])

    def add(self, ev: dict) -> tuple[bool, str]:
        k = self._key(ev)
        for eid, old in list(self.events.items()):
            if self._key(old) == k and k[0] == "addr":
                if old["created_at"] > ev["created_at"]:
                    return True, "duplicate: newer exists"
                del self.events[eid]
        self.events[ev["id"]] = ev
        return True, ""

    @staticmethod
    def match(ev: dict, f: dict) -> bool:
        if "kinds" in f and ev["kind"] not in f["kinds"]:
            return False
        if "authors" in f and ev["pubkey"] not in f["authors"]:
            return False
        if "ids" in f and ev["id"] not in f["ids"]:
            return False
        if "since" in f and ev["created_at"] < f["since"]:
            return False
        if "until" in f and ev["created_at"] > f["until"]:
            return False
        for k, vals in f.items():
            if k.startswith("#") and len(k) == 2:
                have = {t[1] for t in ev["tags"] if t and t[0] == k[1] and len(t) > 1}
                if not have & set(vals):
                    return False
        return True

    async def handler(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "EVENT":
                ok, note = self.add(msg[1])
                await ws.send(json.dumps(["OK", msg[1]["id"], ok, note]))
            elif msg[0] == "REQ":
                sub, flts = msg[1], msg[2:]
                out = [e for e in sorted(self.events.values(), key=lambda e: -e["created_at"])
                       if any(self.match(e, f) for f in flts)]
                limit = min([f.get("limit", 10 ** 6) for f in flts] or [10 ** 6])
                for e in out[:limit]:
                    await ws.send(json.dumps(["EVENT", sub, e]))
                await ws.send(json.dumps(["EOSE", sub]))
            elif msg[0] == "CLOSE":
                pass


def start_in_thread(port: int = 0) -> tuple[Relay, str, threading.Event]:
    """Start a relay on 127.0.0.1 in a daemon thread. Returns (relay, url, stop_event)."""
    relay, ready, stop, box = Relay(), threading.Event(), threading.Event(), {}

    async def main():
        async with websockets.serve(relay.handler, "127.0.0.1", port) as server:
            box["port"] = server.sockets[0].getsockname()[1]
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    ready.wait(5)
    return relay, f"ws://127.0.0.1:{box['port']}", stop


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7447
    relay = Relay()

    async def serve():
        async with websockets.serve(relay.handler, "127.0.0.1", port):
            print(f"mock relay on ws://127.0.0.1:{port}", flush=True)
            await asyncio.Future()
    asyncio.run(serve())
