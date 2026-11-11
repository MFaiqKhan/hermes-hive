# Hive

**Hermes remembers how its errors were fixed. With your OK, Hermes installs share those fixes with each other.**

Every agent re-debugs the same errors. A missing module, a tool that needs a cache rebuilt after an upgrade, a
permission that has to be set once: the agent spends five or ten tool calls finding the fix, and next week, in a
new session, it spends them again. Across thousands of Hermes installs, the same error is solved thousands of
times, alone each time.

Hive fixes both halves:

1. **On your machine.** When a terminal command fails, then the agent changes something, then the same command
   succeeds, Hive records that fix. The evidence is the exit codes, not a model's opinion. The next time that
   error shows up (any session, any folder) Hive adds the fix under the failed command's output:

   ```
   datactl: error: cache index is corrupt or missing (code 17)
   ── Hive: this error has been fixed before ──
   • On this machine (worked 2 times, last 2026-11-12):
       datactl cache rebuild --force
   Hive only suggests; nothing was run. Check it fits before running. (hermes hive show 3f9c0a1d2e)
   ```

2. **Between installs (opt-in).** With `hermes hive network share`, verified fixes are published to
   [Nostr](https://nostr.com) relays: signed, anonymous, no server of ours. Other installs that hit the same error
   see *"3 other Hermes installs fixed it with: …"*. When the fix works for them too, their install vouches for it;
   when it does not, it says so, and the fix sinks.

## How it learns

| Step | What Hive sees | What it does |
|---|---|---|
| 1 | `datactl sync` exits 17 | keeps the failure open for this task, with its *signature* |
| 2 | `datactl --help`, `ls`, `cat …` | ignores them (look-around commands are never fixes) |
| 3 | `datactl cache rebuild --force` exits 0 | remembers it as a candidate step |
| 4 | `datactl sync` exits 0 | **learns**: that error → those steps |

A plain retry that succeeds with nothing changed in between is treated as a flaky failure: nothing is learned.
A changed variant of the failed command that succeeds (`pip install x` → `pip install --user x`) is itself the fix.
If a suggested fix is applied and the same error comes back, Hive counts it as failed.

`hermes hive learn` runs the same learner over your past sessions (read-only) and tells you how many of your
failures were errors you had already fixed before.

## The signature

The error line is picked from the output (the last Python exception line, `error:`/`fatal:`, `E:` from apt,
`npm ERR!`, `command not found` …), then everything that differs between machines or could identify one is
removed: paths, user and host names, URLs, IPs, e-mail addresses, hashes, numbers, long quoted text. Program +
that shape → SHA-256. The same error on two machines gives the same signature:

```
python3 /home/alice/proj/app.py   → ModuleNotFoundError: No module named 'httpx'
python  /Users/bob/work/x/app.py   → ModuleNotFoundError: No module named 'httpx'     (same signature)
```

## Safety: Hive never runs anything

Hints are text. Even so, a hint is a suggestion an agent may follow, so:

- **Never shown:** download piped to a shell, encoded code, deleting whole trees, disk writes, SSH keys, remote
  shells, firewall off, force-push, TLS checks off … (also never learned locally).
- **Shown with a warning:** sudo, global installs, deletes, chmod 777, stopping services, editing shell start-up files.
- **Fixes from other installs must also:**
  - come from at least 2 independent installs (`min_peers`), more yes than no;
  - not install from a URL, a file or a non-default package index;
  - **be explained by the error:** a shared `pip install X` is shown only if `X` appears in the error (or is the
    known package for that import, like `yaml` → `pyyaml`), unless 5+ installs vouch for it. A poisoned
    "`pip install reqeusts`" for a missing `requests` never appears;
  - carry valid signatures, proof of work (NIP-13, 16 bits by default) and content that matches its own hashes.

## Privacy

Network is **off** by default.

| Mode | Leaves your machine |
|---|---|
| `off` | nothing |
| `read` | error *signatures* (hashes) in lookups to the relays |
| `share` | also: the normalized error line, the fix commands, ok/fail counts, OS name |

Before anything is shared:
- home paths become `~` (only dot-folders such as `~/.npmrc` are kept);
- other private paths, non-public hosts, IPs, user names, e-mails and secret-looking text keep the fix
  **private**: it is used on your machine and never published;
- Hermes's own secret redactor gets a final say.

`hermes hive outbox` shows exactly what was or would be published. The install key is random and lives in
`plugin-data/hive/install.key`; nothing ties it to you.

## Commands

```
hermes hive status            what Hive knows; network mode
hermes hive learn [--days N]  learn from past sessions (read-only) and report the repeat rate
hermes hive list | show SIG   learned fixes; one error with local and shared fixes
hermes hive network off|read|share
hermes hive outbox            exactly what is published
hermes hive sync              publish waiting fixes, fetch new reports now
hermes hive relays [URL ...]  set relays (default nos.lol, relay.primal.net, relay.damus.io); --reset
hermes hive forget SIG|--all
/hive                         status in any chat
```

## Install

```
hermes plugins install <this repo>
hermes plugins enable hive
hermes hive learn                # optional: start from your history
hermes hive network share        # optional: join the hive
```

Needs nothing beyond Hermes (pure-Python BIP-340 signatures; `websockets` comes with Hermes).

## Measured

Live runs with a free local model (qwen3.5:4b on Ollama), two throwaway Hermes homes and a local relay
(`tests/run_live_demo.sh`; nothing published publicly). The task: "run `datactl sync` and get it working", where
the error message does not say how to fix it.

| | terminal calls | result |
|---|---|---|
| Machine A, no hint (4 runs) | 5, 7, 8, 10 | 3 fixed, 1 gave up |
| Machine B, Hive hint shown (every run where the error appeared: 3) | 3, 3, 3 | fixed each time: fail → hint → `datactl cache rebuild --force` → sync |

`hermes hive learn` on machine A's own past sessions: 80% of its failures were errors it had already fixed before.
Hook cost: under 0.1 ms per terminal command (no network on the hot path; lookups only on a failure, cached).

## Tests

`python tests/test_hive.py`: 22 offline tests, including three installs trading a fix over a local
mock relay, poisoned fixes (wrong package, `curl | sh`) vouched for by three fake installs that are refused, and the
exact command sequences the live model produced (pipes like `| tee` that hide a failure, `&&` chains, full tool paths).
