"""Hive's brain: learn fixes from what the agent's own commands did, suggest them when the same error returns,
and (opt-in) trade them with other Hermes installs over Nostr.

No Hermes imports at module level, so everything here is testable offline.

How a fix is learned (no model involved; exit codes are the evidence):
  1. A terminal command fails (exit code != 0). Hive keeps it as an open failure for that task.
  2. Commands that change something (installs, config edits, chmod, mkdir ...) run after it are remembered.
  3. The failed command (or a close variant of it) later succeeds. The steps in between, or the variant itself,
     are the fix. Look-around commands (ls, cat, grep, --version ...) never count as fixes.
  4. A plain retry that succeeds with nothing changed in between is treated as a flaky failure: nothing learned.
"""
from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import nostr, safety
from .signature import (_SETUP, command_shape, core_shape, first_segment, error_line, fix_id as make_fix_id, normalize,
                        program, program_by_name, segments, shareable, error_shareable, signature, template_step,
                        canonical_step, masked_failure, expand_vars)
from .store import Store

KIND = 30078                       # NIP-78: application-specific data
TAG = "hermes-hive"
D_PREFIX = "hive/v1/"
DEFAULT_RELAYS = ["wss://nos.lol", "wss://relay.primal.net", "wss://relay.damus.io"]
OPEN_FAILURE_SECONDS = 1800
NOT_A_SHARED_SHAPE = ("it is not a shape other installs accept (an install the error names, or the failed "
                      "command itself with flags added)")
MAX_STEPS = 4

LOOK_AROUND = {
    "ls", "ll", "dir", "cat", "bat", "less", "more", "head", "tail", "echo", "printf", "pwd", "cd", "which", "whereis",
    "type", "file", "stat", "tree", "find", "fd", "grep", "rg", "ag", "wc", "sort", "uniq", "diff", "env", "printenv",
    "ps", "top", "htop", "df", "du", "free", "id", "whoami", "uname", "hostname", "date", "uptime", "history", "man",
    "help", "true", "false", "test", "[", "sleep", "clear", "jq", "awk", "realpath", "readlink", "lsof", "ss",
    "netstat", "nproc", "lscpu", "lsblk", "ping", "dig", "nslookup", "md5sum", "sha256sum",
}
_READ_SUBCOMMANDS = re.compile(r"^\s*(?:git\s+(status|log|diff|show|branch|remote\s+-v|rev-parse)|"
                               r"(pip|pip3)\s+(show|list|freeze|--version)|npm\s+(ls|list|view|--version)|"
                               r"docker\s+(ps|images|inspect|logs)|systemctl\s+(status|is-active|list-units)|"
                               r"apt\s+(list|show|search)|brew\s+(list|info)|journalctl)\b")
_VERSION = re.compile(r"(?:^|\s)(--version|-V|--help|-h|version)(\s|$)")


def is_look_around(cmd: str) -> bool:
    c = cmd.strip()
    if re.search(r"(?<![<>&\d])>>?(?!&)\s*[^\s&|]", c) and not re.search(r">\s*/dev/null", c):
        return False          # a redirect writes a file: that changes something
    prog = program(c)
    named = core_shape(canonical_step(first_segment(c)))
    return (prog in LOOK_AROUND or bool(_READ_SUBCOMMANDS.match(c)) or bool(_READ_SUBCOMMANDS.match(named))
            or bool(_VERSION.search(named) and len(named.split()) <= 4))


def _words(cmd: str) -> set[str]:
    return {w for w in re.split(r"[\s=:,]+", core_shape(cmd)) if w and not w.startswith("-")}


def _tokens_of(cmd: str) -> set[str]:
    return set(core_shape(cmd).split())


def related(cmd: str, failed_cmd: str) -> str:
    """'retry' (same command), 'variant' (same program, mostly the same words) or ''."""
    if command_shape(cmd) == command_shape(failed_cmd) or (core_shape(cmd) and core_shape(cmd) == core_shape(failed_cmd)):
        return "retry"
    if program(cmd) and program(cmd) == program(failed_cmd) and not is_look_around(cmd):
        a, b = _words(cmd), _words(failed_cmd)
        if a and b and len(a & b) / len(a | b) >= 0.5:
            return "variant"
    return ""


def failed_part(command: str, prog: str) -> str:
    """The part of a command list that failed: in `cat notes && export X=1 && datactl sync` the error came from
    datactl, so the failure is `datactl sync`. Keeping the whole list would make a later `cat other-file` look
    like a successful retry of it."""
    parts = segments(command)
    if len(parts) < 2 or not prog:
        return command
    return next((p for p in reversed(parts) if program(p) == prog), command)


def network_shape(command: str, prog: str, home: Optional[str] = None) -> str:
    """The command that failed, written the way a fix step is written before it is shared: the failed part of a
    list, the program by name, output plumbing dropped, the home folder as ~. A fix from another install may only
    be this command plus flags (`safety.network_step_allowed`). "" when it is not one plain command."""
    part = failed_part(expand_vars(command or ""), prog)
    if not prog or len(segments(part)) != 1:
        return ""
    part = canonical_step(program_by_name(part, prog))
    if re.search(r"[|<>`]|\$\(", part) or program(part) != prog:
        return ""
    return template_step(part, home)


@dataclass
class Failure:
    sig: str
    program: str
    error: str
    command: str
    at: float
    steps: list = field(default_factory=list)
    hinted: list = field(default_factory=list)      # fix ids shown for this failure


@dataclass
class Learned:
    sig: str
    program: str
    error: str
    steps: list
    fix_id: str
    new: bool
    shareable: bool
    note: str


@dataclass
class Settings:
    """Stored in the plugin's own SQLite file and changed with `hermes hive network|relays|settings`."""
    network: str = "off"                    # off | read | share
    relays: list = field(default_factory=lambda: list(DEFAULT_RELAYS))
    min_pow: int = 16
    min_peers: int = 2
    lookup_timeout: float = 2.5
    lookup_every: float = 3600.0

    FIELDS = ("network", "relays", "min_pow", "min_peers", "lookup_timeout", "lookup_every")

    @classmethod
    def load(cls, store: Store) -> "Settings":
        s = cls()
        saved = store.get_meta("settings", {}) or {}
        for k, v in saved.items():
            if k in cls.FIELDS:
                setattr(s, k, v)
        return s

    def save(self, store: Store) -> None:
        store.set_meta("settings", {k: getattr(self, k) for k in self.FIELDS})


class Hive:
    def __init__(self, data_dir: Path, *, clock: Callable[[], float] = time.time, home: Optional[str] = None):
        self.data_dir = Path(data_dir)
        self.store = Store(self.data_dir / "hive.db")
        self.settings = Settings.load(self.store)
        self.clock = clock
        self.home = home or os.path.expanduser("~")
        self.os_name = platform.system().lower() or "unknown"
        self._open: dict[str, list[Failure]] = defaultdict(list)
        self._lock = threading.RLock()
        self._key: Optional[bytes] = None
        self._sync_thread: Optional[threading.Thread] = None
        self.stats = {"failures": 0, "hints": 0, "learned": 0}
        self._source = "live"
        self._last_learned: list = []

    # ── identity ──
    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = nostr.load_or_create_key(self.data_dir / "install.key")
        return self._key

    @property
    def pubkey(self) -> str:
        return nostr.pubkey_of(self.key).hex()

    # ── live observation ──
    def observe(self, command: str, output: str, returncode: Optional[int], task: str = "") -> Optional[str]:
        """Feed one finished terminal command. Returns a hint to append to the output, or None."""
        self._source = "live"
        return self._process(command, output or "", returncode, task or "default", self.clock(), live=True)

    def _process(self, command: str, output: str, returncode: Optional[int], task: str, now: float,
                 live: bool) -> Optional[str]:
        self._last_learned = []
        if live and returncode not in (0, None):
            self.settings = Settings.load(self.store)   # `hermes hive network ...` may have changed it meanwhile
        command = expand_vars(command or "")
        if returncode is None or not (command or "").strip():
            return None
        with self._lock:
            self._expire(task, now)
            if returncode == 0 and masked_failure(command, output):
                returncode = 1                   # `cmd | tee log` exits 0 even when cmd failed
            if returncode != 0:
                self._resolve_chain(command, output, task, now)
                return self._on_failure(command, output, task, now, live)
            parts = segments(command)
            if len(parts) > 1:               # `fix && retry` in one command: each part ran and succeeded
                for part in parts:
                    self._on_success(part, task, now)
            else:
                self._on_success(command, task, now)
            return None

    def _expire(self, task: str, now: float) -> None:
        self._open[task] = [f for f in self._open[task] if now - f.at <= OPEN_FAILURE_SECONDS]

    def _resolve_chain(self, command: str, output: str, task: str, now: float) -> None:
        """`a && b` failed in b, so a ran and exited 0. If a retries an open failure whose error did not come
        back, that failure is solved; the parts before a count as steps."""
        segs = segments(command)
        if len(segs) < 2 or "&&" not in command:
            return
        shown = normalize(output)
        for i, seg in enumerate(segs[:-1]):
            if any(related(seg, f.command) and f.error not in shown for f in self._open[task]):
                for s in segs[:i + 1]:
                    self._on_success(s, task, now)
                return

    def _on_failure(self, command: str, output: str, task: str, now: float, live: bool = True) -> Optional[str]:
        sig, prog, shape = signature(command, output)
        if not shape or prog in _SETUP:
            return None                      # a failed `cd`/`export` is set-up, not the program's error
        self.stats["failures"] += 1
        # The same error came back after a hinted fix was applied: that fix did not work here.
        for f in self._open[task]:
            if f.sig == sig and f.hinted and any(related(s, step) == "retry"
                                                 for s in f.steps for fid in f.hinted
                                                 for step in self._fix_steps(sig, fid)):
                for fid in f.hinted:
                    self._record_fail(sig, fid, prog, shape, network_shape(f.command, prog, self.home))
                f.hinted = []
        command = failed_part(command, prog)
        existing = next((f for f in self._open[task] if f.sig == sig), None)
        if existing:
            existing.at, existing.command = now, command
        else:
            self._open[task].append(Failure(sig, prog, shape, command, now))
        failed = network_shape(command, prog, self.home)
        if failed:
            self.store.saw(sig, failed, now)          # so `hermes hive show` judges network text the same way
        if not live:
            return None
        hint, shown = self.hint(sig, prog, shape, failed)
        if shown:
            tgt = existing or self._open[task][-1]
            tgt.hinted = shown
            self.stats["hints"] += 1
        return hint

    def _on_success(self, command: str, task: str, now: float) -> None:
        for f in list(reversed(self._open[task])):
            rel = related(command, f.command)
            if rel == "retry":
                if f.steps:
                    self._learn(f, f.steps, now, source=self._source)
                self._open[task].remove(f)
            elif rel == "variant":
                # `datactl sync` after `datactl sync --repair-index` failed: if work was done in between and this
                # command only drops words, it is the plain retry, and the work in between is the fix
                # (a command that ADDS a flag, `datactl sync --rebuild-index`, is itself part of the fix)
                if f.steps and _tokens_of(command) <= _tokens_of(f.command):
                    self._learn(f, f.steps, now, source=self._source)
                else:
                    self._learn(f, f.steps + [command], now, source=self._source)
                self._open[task].remove(f)
            elif not is_look_around(command) and command not in f.steps:
                f.steps = (f.steps + [command])[-MAX_STEPS:]

    def _fix_steps(self, sig: str, fid: str) -> list[str]:
        row = self.store.antibody(sig, fid)
        if row:
            return json.loads(row["fix"])
        v = next((v for v in self.store.votes(sig) if v["fix_id"] == fid), None)
        return json.loads(v["fix"]) if v else []

    def essential_steps(self, f: Failure, raw_steps: list[str]) -> list[str]:
        """The part of the work after a failure that most likely fixed it.

        Compound commands are split; set-up (cd, export) and look-around parts are dropped. When some steps run
        the same program as the failed command (`datactl cache rebuild --force` after `datactl sync` failed),
        those are the fix and anything else (an agent's own workaround) is left out. Otherwise the last two
        changing steps are kept."""
        parts: list[str] = []
        for raw in raw_steps:
            for seg in segments(expand_vars(raw)):
                head = seg.split()[0] if seg.split() else ""
                if os.path.basename(head) in _SETUP or is_look_around(seg):
                    continue
                seg = canonical_step(program_by_name(seg, program(seg)))
                if seg and seg not in parts:
                    parts.append(seg)
        same = [p for p in parts if program(p) == f.program]
        return same if same else parts[-2:]

    def _learn(self, f: Failure, raw_steps: list[str], now: float, source: str = "live") -> Optional[Learned]:
        self.settings = Settings.load(self.store)
        raw_steps = self.essential_steps(f, raw_steps)
        steps = [template_step(s, self.home) for s in raw_steps]
        steps = [s for s in steps if s]
        if not steps:
            return None
        verdicts = [safety.screen(s, "local")[0] for s in steps]
        if "block" in verdicts:
            return None                      # never keep a dangerous "fix", even locally
        fid = make_fix_id(steps)
        failed = network_shape(f.command, f.program, self.home)
        checks = [shareable(s) for s in steps] + [error_shareable(f.error)]
        if all(c[0] for c in checks) and not all(safety.network_step_allowed(s, f.error, failed) for s in steps):
            checks.append((False, NOT_A_SHARED_SHAPE))
        ok_share = all(c[0] for c in checks)
        note = next((c[1] for c in checks if not c[0]), "")
        new = self.store.learn(f.sig, fid, f.program, f.error, steps, source=source, os_name=self.os_name,
                               shareable=ok_share, share_note=note, at=now, command=failed)
        self.stats["learned"] += 1
        if ok_share and self.settings.network == "share":
            self.store.queue(f.sig, fid)
            self._kick_sync()
        item = Learned(f.sig, f.program, f.error, steps, fid, new, ok_share, note)
        self._last_learned.append(item)
        return item

    def _record_fail(self, sig: str, fid: str, prog: str, shape: str, failed: str = "") -> None:
        if self.store.antibody(sig, fid):
            self.store.outcome(sig, fid, worked=False)
        else:
            steps = self._fix_steps(sig, fid)
            if steps:
                ok_share = (all(shareable(s)[0] for s in steps) and error_shareable(shape)[0]
                            and all(safety.network_step_allowed(s, shape, failed) for s in steps))
                self.store.learn(sig, fid, prog, shape, steps, source="hive", os_name=self.os_name,
                                 shareable=ok_share, share_note="", ok=0, command=failed)
                self.store.outcome(sig, fid, worked=False)
        if self.settings.network == "share" and self.store.antibody(sig, fid)["shareable"]:
            self.store.queue(sig, fid)
            self._kick_sync()

    # ── hints ──
    def candidates(self, sig: str, error: str, failed: str = "") -> list[dict]:
        """Fixes to show for an error. `failed` is the command that just failed here (`network_shape`); text
        from other installs is judged against it."""
        out = []
        for r in self.store.local_fixes(sig):
            if r["ok"] <= r["fail"]:
                continue
            steps = json.loads(r["fix"])
            verdicts = [safety.screen(s, "local") for s in steps]
            if any(v[0] == "block" for v in verdicts):
                continue
            out.append({"fix_id": r["fix_id"], "steps": steps, "origin": "local", "ok": r["ok"], "fail": r["fail"],
                        "last": r["last_seen"], "warn": sorted({w for v in verdicts for w in v[1]})})
        if self.settings.network != "off":
            mine = self._safe_pubkey()
            groups: dict[str, dict] = {}
            for v in self.store.votes(sig):
                if v["pubkey"] == mine:
                    continue
                g = groups.setdefault(v["fix_id"], {"steps": json.loads(v["fix"]), "yes": 0, "no": 0})
                if v["ok"] > v["fail"]:
                    g["yes"] += 1
                elif v["fail"] > 0:
                    g["no"] += 1
            local_ids = {c["fix_id"] for c in out}
            for fid, g in groups.items():
                if fid in local_ids or g["yes"] < self.settings.min_peers or g["yes"] <= g["no"]:
                    continue
                verdicts = [safety.screen(s, "network") for s in g["steps"]]
                if any(v[0] == "block" for v in verdicts):
                    continue
                # Votes cost nothing to fake, so they never vouch for a shape on their own: every step must be
                # an install the error names or this machine's own failed command plus flags, however many
                # installs report it.
                if not all(safety.network_step_allowed(s, error, failed) for s in g["steps"]):
                    continue
                out.append({"fix_id": fid, "steps": g["steps"], "origin": "network", "peers": g["yes"],
                            "against": g["no"], "warn": sorted({w for v in verdicts for w in v[1]})})
        out.sort(key=lambda c: (c["origin"] != "local", -(c.get("ok", 0) - c.get("fail", 0)),
                                -(c.get("peers", 0) - c.get("against", 0))))
        return out[:3]

    def hint(self, sig: str, prog: str, error: str, failed: str = "") -> tuple[Optional[str], list[str]]:
        if self.settings.network != "off" and not self.store.looked_up(sig, self.settings.lookup_every):
            try:
                self.lookup(sig, timeout=self.settings.lookup_timeout)
            except Exception:  # noqa: BLE001 - the network is a bonus; a failure never blocks the agent
                pass
        cands = self.candidates(sig, error, failed)
        if not cands:
            return None, []
        lines = ["", "── Hive: this error has been fixed before ──"]
        for c in cands:
            if c["origin"] == "local":
                when = time.strftime("%Y-%m-%d", time.localtime(c["last"])) if c.get("last") else "earlier"
                times = f"{c['ok']} time{'s' if c['ok'] != 1 else ''}"
                lines.append(f"• On this machine (worked {times}, last {when}):")
            else:
                against = f", {c['against']} said it did not" if c.get("against") else ""
                who = "1 anonymous Nostr peer" if c["peers"] == 1 else f"{c['peers']} anonymous Nostr peers"
                lines.append(f"• Unverified text from {who}{against}; check it before running anything:")
            lines += [f"    {s}" for s in c["steps"]]
            if c["warn"]:
                lines.append(f"  ⚠ careful: {', '.join(c['warn'])}")
        if cands[0]["origin"] == "local":
            first = cands[0]["steps"]
            lines.append(f"Suggested next step: run `{' && '.join(first)}`, then retry the command that failed.")
        lines.append(f"(Hive only suggests; nothing was run. Details: hermes hive show {sig[:10]})")
        return "\n".join(lines), [c["fix_id"] for c in cands]

    # ── history ──
    def learn_from_history(self, db_path: Path, since: Optional[float] = None) -> dict:
        """Replay terminal commands from a Hermes state.db (opened read-only) through the same learner."""
        report = {"sessions": 0, "commands": 0, "failures": 0, "learned": 0, "new": 0, "repeat_failures": 0,
                  "repeat_known": 0}
        seen_sigs: dict[str, float] = {}
        fixed_sigs: set[str] = set()
        for sid, seq in iter_terminal_history(db_path, since):
            report["sessions"] += 1
            task = f"history:{sid}"
            for cmd, rc, out, at in seq:
                report["commands"] += 1
                if rc is None:
                    continue
                if rc != 0 or masked_failure(cmd, out):
                    sig, prog, shape = signature(cmd, out)
                    if shape and prog not in _SETUP:
                        report["failures"] += 1
                        report["repeat_failures"] += int(sig in seen_sigs)
                        report["repeat_known"] += int(sig in fixed_sigs)
                        seen_sigs.setdefault(sig, at)
                self._source = "history"
                self._process(cmd, out or "", rc, task, at, live=False)
                for item in self._last_learned:
                    report["learned"] += 1
                    report["new"] += int(item.new)
                    fixed_sigs.add(item.sig)
            self._open.pop(task, None)
        return report

    # ── network ──
    def _safe_pubkey(self) -> str:
        try:
            return self.pubkey
        except Exception:  # noqa: BLE001
            return ""

    def build_event(self, row) -> dict:
        content = {"v": 1, "sig": row["sig"], "fix_id": row["fix_id"], "program": row["program"],
                   "error": row["error"], "fix": json.loads(row["fix"]), "ok": row["ok"], "fail": row["fail"],
                   "os": row["os"]}
        tags = [["d", f"{D_PREFIX}{row['sig']}/{row['fix_id']}"], ["t", TAG], ["t", f"hh-{row['sig']}"],
                ["prog", row["program"] or ""], ["os", row["os"] or ""], ["client", "hermes-hive"]]
        return nostr.make_event(self.key, KIND, tags, json.dumps(content, separators=(",", ":")),
                                pow_bits=self.settings.min_pow)

    def _own_pubkey(self) -> str:
        """This install's key, if it has one (an install without a key never published anything)."""
        if self._key is None and not (self.data_dir / "install.key").exists():
            return ""
        return self._safe_pubkey()

    def _forget_own_reports(self) -> str:
        own = self._own_pubkey()
        if own:
            self.store.drop_votes_from(own)          # rows stored by earlier versions, which kept them
        return own

    def accept_event(self, ev: dict, own: Optional[str] = None) -> bool:
        """Validate one event from a relay and store it as a vote. False when it is not a valid Hive event, or when
        it is this install's own report coming back from a relay (it is not another install's evidence)."""
        try:
            if ev.get("kind") != KIND or not nostr.verify_event(ev, min_pow=self.settings.min_pow):
                return False
            if ev.get("pubkey") == (self._own_pubkey() if own is None else own):
                return False
            d = next((t[1] for t in ev["tags"] if t and t[0] == "d"), "")
            c = json.loads(ev["content"])
            if not d.startswith(D_PREFIX) or c.get("v") != 1:
                return False
            sig, fid, prog, err, fix = c["sig"], c["fix_id"], c["program"], c["error"], c["fix"]
            if not (isinstance(fix, list) and 0 < len(fix) <= MAX_STEPS and all(isinstance(s, str) and len(s) <= 300
                                                                               for s in fix)):
                return False
            if len(err) > 300 or d != f"{D_PREFIX}{sig}/{fid}":
                return False
            import hashlib
            if hashlib.sha256(f"{prog}\n{err}".encode()).hexdigest()[:32] != sig or make_fix_id(fix) != fid:
                return False                 # content must match the hashes it claims
            ok, fail = int(c.get("ok", 0)), int(c.get("fail", 0))
            if ok < 0 or fail < 0:
                return False
            return self.store.add_vote(sig, fid, ev["pubkey"], min(ok, 1000), min(fail, 1000), prog, err, fix,
                                       str(c.get("os", ""))[:20], int(ev["created_at"]), ev["id"]) or True
        except Exception:  # noqa: BLE001
            return False

    def lookup(self, sig: str, timeout: float = 2.5) -> int:
        events, _errors = nostr.query(self.settings.relays, {"kinds": [KIND], "#t": [f"hh-{sig}"], "limit": 200},
                                      timeout=timeout)
        self.store.mark_lookup(sig)
        own = self._forget_own_reports()
        return sum(1 for ev in events if self.accept_event(ev, own))

    def publish_pending(self, limit: int = 20) -> list[dict]:
        results = []
        for row in self.store.pending()[:limit]:
            if not row["shareable"]:
                continue
            if not self.still_shareable(row):         # learned under an older, wider rule: it stays local
                self.store.keep_private(row["sig"], row["fix_id"], NOT_A_SHARED_SHAPE)
                continue
            ev = self.build_event(row)
            res = nostr.publish(self.settings.relays, ev)
            okays = [r for r, ok, _ in res if ok]
            if okays:
                self.store.published(row["sig"], row["fix_id"], ev["id"], okays)
            results.append({"sig": row["sig"], "fix_id": row["fix_id"], "event": ev["id"], "relays": res})
        return results

    def still_shareable(self, row) -> bool:
        """A stored fix checked against today's rules: every step an install the error names, or the failed
        command (recorded when it was learned) plus flags."""
        return all(safety.network_step_allowed(s, row["error"], row["command"] or "") for s in json.loads(row["fix"]))

    def pull(self, timeout: float = 6.0) -> int:
        since = int(self.store.get_meta("pulled_until", 0) or 0)
        flt = {"kinds": [KIND], "#t": [TAG], "limit": 500}
        if since:
            flt["since"] = max(0, since - 600)
        events, _errors = nostr.query(self.settings.relays, flt, timeout=timeout)
        own = self._forget_own_reports()
        n = sum(1 for ev in events if self.accept_event(ev, own))
        self.store.set_meta("pulled_until", int(time.time()))
        return n

    def sync(self) -> dict:
        out = {"published": [], "pulled": 0}
        if self.settings.network == "share":
            out["published"] = self.publish_pending()
        if self.settings.network in ("read", "share"):
            out["pulled"] = self.pull()
        return out

    def _kick_sync(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            return

        def run():
            try:
                self.publish_pending()
            except Exception:  # noqa: BLE001
                pass
        self._sync_thread = threading.Thread(target=run, name="hive-publish", daemon=True)
        self._sync_thread.start()

    def start_background(self, every: float = 600.0) -> None:
        if self.settings.network == "off":
            return

        def loop():
            while True:
                try:
                    self.sync()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(every)
        threading.Thread(target=loop, name="hive-sync", daemon=True).start()


# ── reading Hermes's session store ────────────────────────────────────────────────────────────────────────────

def iter_terminal_history(db_path: Path, since: Optional[float] = None):
    """Yield (session_id, [(command, exit_code, output, timestamp), ...]) from a Hermes state.db, read-only."""
    import sqlite3
    uri = f"file:{Path(db_path)}?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True)
        db.execute("SELECT 1 FROM messages LIMIT 1")
    except sqlite3.DatabaseError:
        db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    q = "SELECT session_id, id, role, content, tool_calls, tool_call_id, tool_name, timestamp FROM messages"
    args: tuple = ()
    if since:
        q += " WHERE timestamp >= ?"
        args = (since,)
    q += " ORDER BY session_id, id"
    current, calls, seq = None, {}, []
    for r in db.execute(q, args):
        if r["session_id"] != current:
            if current is not None and seq:
                yield current, seq
            current, calls, seq = r["session_id"], {}, []
        if r["role"] == "assistant" and r["tool_calls"]:
            try:
                for tc in json.loads(r["tool_calls"]) or []:
                    fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                    if fn.get("name") == "terminal":
                        args_ = fn.get("arguments")
                        args_ = json.loads(args_) if isinstance(args_, str) else (args_ or {})
                        if args_.get("command") and not args_.get("background"):
                            calls[tc.get("id")] = args_["command"]
            except (ValueError, TypeError, AttributeError):
                pass
        elif r["role"] == "tool" and r["tool_call_id"] in calls:
            try:
                body = json.loads(r["content"])
                code = body.get("exit_code")
                out = (body.get("output") or "") + ("\n" + body["error"] if body.get("error") else "")
            except (ValueError, TypeError, AttributeError):
                continue
            ts = r["timestamp"]
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                ts = 0.0
            seq.append((calls.pop(r["tool_call_id"]), code if isinstance(code, int) else None, out, ts))
    if current is not None and seq:
        yield current, seq
    db.close()
