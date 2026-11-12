"""Hive: Hermes remembers how errors were fixed, and (opt-in) Hermes installs share those fixes over Nostr.

Hook: transform_terminal_output. Every finished terminal command passes through Hive; a failure whose error is
already known gets a short "fixed before" note under its output. Nothing is ever run by Hive.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import safety
from .engine import DEFAULT_RELAYS, Hive

_HIVE: Optional[Hive] = None


def _data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("hive")
    except Exception:  # noqa: BLE001 - outside Hermes (tests) or an older Hermes
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        p = home / "plugin-data" / "hive"
        p.mkdir(parents=True, exist_ok=True)
        return p


def hive() -> Hive:
    global _HIVE
    if _HIVE is None or _HIVE.data_dir != _data_dir():
        _HIVE = Hive(_data_dir())
    return _HIVE


def _state_db() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()) / "state.db"
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "state.db"


# ── hook ──
def _on_terminal_output(command: str = "", output: str = "", returncode: Optional[int] = None, task_id: str = "",
                        **_kw: Any) -> Optional[str]:
    try:
        note = hive().observe(command, output, returncode, task_id or "")
    except Exception:  # noqa: BLE001 - Hive must never break a terminal call
        return None
    if not note:
        return None
    return (output or "").rstrip("\n") + "\n" + note


# ── text ──
def _when(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def status_text() -> str:
    h = hive()
    abs_ = h.store.all_antibodies()
    local = [a for a in abs_ if a["ok"] > a["fail"]]
    keys, sigs = h.store.vote_count()
    pend = [r for r in h.store.outbox_rows() if not r["published_at"]]
    def n(k, word, plural=None):
        return f"{k} {word if k == 1 else (plural or word + 's')}"
    lines = [f"Hive: {n(len(local), 'fix', 'fixes')} learned on this machine ({len(abs_)} recorded)."]
    net = h.settings.network
    if net == "off":
        lines.append("Network: off (fixes stay on this machine). Turn on with: hermes hive network read|share")
    else:
        lines.append(f"Network: {net}. {n(keys, 'other install')} seen, sharing fixes for {n(sigs, 'error')}. "
                     f"Relays: {', '.join(h.settings.relays)}")
        if net == "share":
            lines.append(f"Waiting to publish: {len(pend)}. Your install key: {h.pubkey[:16]}… (random, not you)")
    if local:
        lines.append("Most recent:")
        for a in local[:5]:
            fix = " && ".join(json.loads(a["fix"]))
            lines.append(f"  {a['sig'][:10]}  {a['program']}: {a['error'][:70]}\n      fix: {fix[:110]}  "
                         f"(worked {a['ok']}x)")
    return "\n".join(lines)


def _reason(note: Optional[str]) -> str:
    """A share note as a sentence: "mentions a private path" -> "it mentions a private path"."""
    note = note or "mentions private details"
    return note if note.startswith(("the ", "it ", "Hermes")) else "it " + note


def _show(sig: str) -> str:
    h = hive()
    rows = [a for a in h.store.all_antibodies() if a["sig"].startswith(sig)]
    votes = [v for v in h.store.rows("SELECT * FROM votes WHERE sig LIKE ?", (sig + "%",))]
    if not rows and not votes:
        return f"No fix known for {sig}."
    out = []
    err = (rows[0]["error"] if rows else votes[0]["error"])
    prog = (rows[0]["program"] if rows else votes[0]["program"])
    out.append(f"Error ({prog}): {err}")
    for a in rows:
        out.append(f"\nThis machine: worked {a['ok']}x, failed {a['fail']}x, learned {_when(a['first_seen'])} "
                   f"({a['source']}); share: {'yes' if a['shareable'] else 'no - ' + (a['share_note'] or '')}")
        out += [f"    {s}" for s in json.loads(a["fix"])]
    by_fix: dict[str, list] = {}
    mine = h._safe_pubkey()
    for v in votes:
        if v["pubkey"] != mine:
            by_fix.setdefault(v["fix_id"], []).append(v)
    refused = 0
    for fid, vs in by_fix.items():
        steps = json.loads(vs[0]["fix"])
        # The hint points agents here, so text Hive would not show in a hint is not shown here either.
        if any(safety.screen(s, "network")[0] == "block" for s in steps) or \
                not all(safety.network_step_allowed(s, vs[0]["error"], prog) for s in steps):
            refused += len(vs)
            continue
        yes = sum(1 for v in vs if v["ok"] > v["fail"])
        no = sum(1 for v in vs if v["fail"] >= v["ok"] and v["fail"] > 0)
        out.append(f"\nUnverified text from anonymous Nostr peers ({yes} say it works, {no} say it does not); "
                   f"check it before running anything:")
        out += [f"    {s}" for s in steps]
    if refused:
        out.append(f"\nRefused: {refused} report{'s' if refused != 1 else ''} from anonymous peers with commands in a "
                   f"shape Hive does not accept from the network (not shown).")
    return "\n".join(out)


# ── CLI ──
def _setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="hive_cmd")
    sub.add_parser("status", help="What Hive knows and whether it talks to the network")
    p = sub.add_parser("learn", help="Learn fixes from past sessions (reads state.db read-only)")
    p.add_argument("--db", help="Path to a Hermes state.db (default: this profile's)")
    p.add_argument("--days", type=int, help="Only the last N days")
    p.add_argument("--json", action="store_true")
    sub.add_parser("list", help="All fixes learned on this machine")
    p = sub.add_parser("show", help="One error: its fixes here and from other installs")
    p.add_argument("sig")
    p = sub.add_parser("network", help="off (default) | read (use others' fixes) | share (also publish yours)")
    p.add_argument("mode", choices=["off", "read", "share"])
    sub.add_parser("outbox", help="Exactly what was or would be published")
    sub.add_parser("sync", help="Publish waiting fixes and fetch new ones now")
    p = sub.add_parser("relays", help="Show or set the Nostr relays")
    p.add_argument("urls", nargs="*")
    p.add_argument("--reset", action="store_true")
    p = sub.add_parser("settings", help="Show or change network settings")
    p.add_argument("--min-peers", type=int, help="independent installs a shared fix needs before it is shown (default 2)")
    p.add_argument("--min-pow", type=int, help="proof-of-work bits required on shared fixes (default 16)")
    p = sub.add_parser("forget", help="Delete learned fixes (one signature prefix, or --all)")
    p.add_argument("sig", nargs="?")
    p.add_argument("--all", action="store_true")


def _handle_cli(args: argparse.Namespace) -> int:
    h = hive()
    cmd = getattr(args, "hive_cmd", None) or "status"
    if cmd == "status":
        print(status_text())
    elif cmd == "learn":
        db = Path(args.db) if args.db else _state_db()
        if not db.exists():
            print(f"No session database at {db}")
            return 1
        since = time.time() - args.days * 86400 if args.days else None
        rep = h.learn_from_history(db, since)
        if args.json:
            print(json.dumps(rep))
        else:
            pct = (100 * rep["repeat_known"] / rep["failures"]) if rep["failures"] else 0
            print(f"Read {rep['commands']} terminal command{'s' if rep['commands'] != 1 else ''} in {rep['sessions']} "
                  f"session{'s' if rep['sessions'] != 1 else ''} (read-only).")
            print(f"Failures: {rep['failures']}. Fixes learned: {rep['learned']} ({rep['new']} new).")
            print(f"Failures of an error seen before: {rep['repeat_failures']}; of an error already fixed before: "
                  f"{rep['repeat_known']} ({pct:.0f}% of all failures). Hive would have pointed to the fix.")
            if h.settings.network == "share":
                print(f"Queued to share: {len(h.store.pending())} (run: hermes hive outbox)")
    elif cmd == "list":
        rows = h.store.all_antibodies()
        if not rows:
            print("Nothing learned yet.")
        for a in rows:
            print(f"{a['sig'][:10]}  ok {a['ok']} fail {a['fail']}  {a['program']}: {a['error'][:80]}")
            print(f"            fix: {' && '.join(json.loads(a['fix']))[:120]}")
    elif cmd == "show":
        print(_show(args.sig))
    elif cmd == "network":
        h.settings.network = args.mode
        h.settings.save(h.store)
        if args.mode == "share":
            for a in h.store.all_antibodies():
                if a["shareable"] and a["ok"] > a["fail"]:
                    h.store.queue(a["sig"], a["fix_id"])
        msg = {"off": "Hive stays on this machine.",
               "read": "Hive will look up other installs' fixes (it sends only error fingerprints to the relays).",
               "share": "Hive will also publish your verified fixes. Preview them first: hermes hive outbox"}
        print(f"Network: {args.mode}. {msg[args.mode]}")
    elif cmd == "outbox":
        rows = h.store.outbox_rows()
        if not rows:
            print("Nothing queued. (Fixes are queued when network is 'share'.)")
        for r in rows:
            state = f"published {_when(r['published_at'])} as {r['event_id'][:12]}…" if r["published_at"] else "waiting"
            print(f"[{state}] {r['program']}: {r['error'][:90]}")
            print(json.dumps({"error": r["error"], "fix": json.loads(r["fix"]), "ok": r["ok"], "fail": r["fail"],
                              "os": r["os"]}, ensure_ascii=False))
        private = [a for a in h.store.all_antibodies() if not a["shareable"]]
        if private:
            print(f"\nKept private (never published): {len(private)} fix{'es' if len(private) != 1 else ''}. "
                  f"Reason{' for the first' if len(private) != 1 else ''}: "
                  f"{_reason(private[0]['share_note'])}.")
    elif cmd == "sync":
        if h.settings.network == "off":
            print("Network is off. Turn on with: hermes hive network read|share")
            return 1
        res = h.sync()
        for p in res["published"]:
            oks = [r for r, ok, _ in p["relays"] if ok]
            print(f"published {p['sig'][:10]} to {len(oks)}/{len(p['relays'])} relays")
        print(f"fetched {res['pulled']} fix reports from other installs")
    elif cmd == "relays":
        if args.reset:
            h.settings.relays = list(DEFAULT_RELAYS)
        elif args.urls:
            h.settings.relays = [u if u.startswith("ws") else f"wss://{u}" for u in args.urls]
        h.settings.save(h.store)
        print("Relays:", ", ".join(h.settings.relays))
    elif cmd == "settings":
        if args.min_peers is not None:
            h.settings.min_peers = max(1, args.min_peers)
        if args.min_pow is not None:
            h.settings.min_pow = max(0, args.min_pow)
        h.settings.save(h.store)
        print(json.dumps({k: getattr(h.settings, k) for k in h.settings.FIELDS}, indent=2))
    elif cmd == "forget":
        if not args.sig and not args.all:
            print("Give a signature prefix or --all")
            return 1
        print(f"Forgot {h.store.forget(None if args.all else args.sig)} fixes.")
    return 0


def _slash(raw_args: str = "", **_kw: Any) -> str:
    return status_text()


def register(ctx) -> None:
    ctx.register_hook("transform_terminal_output", _on_terminal_output)
    ctx.register_cli_command(
        name="hive", help="Fixes Hermes learned from its own errors, shared with other installs if you want",
        setup_fn=_setup_cli, handler_fn=_handle_cli,
        description="Hive remembers which command fixed which error and points to it when the error comes back. "
                    "Start with: hermes hive learn")
    ctx.register_command("hive", _slash, description="What Hive has learned")
    try:
        hive().start_background()
    except Exception:  # noqa: BLE001
        pass
