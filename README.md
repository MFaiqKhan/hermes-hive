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
   see it labelled for what it is: *"Unverified text from 2 anonymous Nostr peers; check it before running
   anything"*. Only fixes learned on your own machine are phrased as a suggested next step. When a shared fix works
   for another install too, that install vouches for it; when it does not, it says so, and the fix sinks.

## How it learns

| Step | What Hive sees | What it does |
|---|---|---|
| 1 | `datactl sync` exits 17 | keeps the failure open for this task, with its *signature* |
| 2 | `datactl --help`, `ls`, `cat …` | ignores them (look-around commands are never fixes) |
| 3 | `datactl cache rebuild --force` exits 0 | remembers it as a candidate step |
| 4 | `datactl sync` exits 0 | **learns**: that error → those steps |

A plain retry that succeeds with nothing changed in between is treated as a flaky failure: nothing is learned.
A changed variant of the failed command that succeeds (`pip install x` → `pip install --user x`,
`datactl sync` → `datactl sync --rebuild-index`) is itself the fix.
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
- **Fixes from other installs** are text from anonymous strangers, and votes from random keys cost nothing to
  fake. So a shared fix is shown only when **every step has one of two allowlisted shapes**, however many installs
  report it:
  1. a known installer installing packages **named in the error** (`pip install httpx` for
     "No module named 'httpx'"; `yaml` → `pyyaml` style import names are known, and for those the import name
     itself is refused: `pip install yaml` or `pip install sklearn` would install whatever is registered under that
     name); never from a URL, a file or a non-default index, never with `--break-system-packages`;
  2. **the exact command that failed on your machine, with plain flags added at the end** (`npm install` →
     `npm install --legacy-peer-deps`, `datactl sync` → `datactl sync --rebuild-index`). The command is compared
     token for token, so a stranger never chooses the program, the subcommand or an argument, and cannot insert,
     remove or reorder anything. A flag is a plain switch or a long switch with a number (`--rebuild-index`, `-v`,
     `--jobs=1`); a value that could name a package, file, host or URL is not accepted (`--package=x`,
     `--registry=x`, `--config=x`), nor is a switch that turns off a TLS, signature, sandbox, permission or
     system-package check, skips a confirmation, or (outside dependency maintenance such as `npm install --force`)
     forces anything (`--force`, `-f`).
     This shape is never used for interpreters, shells and package runners, for programs whose flags alone can
     run code, reach another machine or destroy data (`find`, `tar`, `ssh`, `rsync`, `awk`, `sed`, `docker`,
     `rm`, `chmod`, `dropdb`, `kill`, `wipefs`, `iptables`, `userdel` …), or for fetch-and-run subcommands (`exec`, `x`, `dlx`, `create`, `init`, `run`, `start`,
     `test`, `download`, `wheel` …). For package managers and build tools (`npm`, `pnpm`, `yarn`, `go`, `cargo`,
     `pip`, `mvn`, `gradle` …) it covers only dependency-maintenance subcommands (`install`, `ci`, `update`,
     `cache` …), because they run project code under almost any other name.

  So after an `npm`, `go`, `cargo` or `pip` failure, `npm exec <pkg>`, `pnpm dlx <pkg>`, `go run <mod>@latest`,
  `cargo run` and `pip download <sdist>` are refused whatever the votes. A fix that changes the subcommand
  (`datactl cache rebuild --force` after `datactl sync` failed) is still learned and used on the machine that
  found it; it is just never shared and never accepted from others.

  Package names must match the error **as whole words**: `colorama` in the error does not justify
  `pip install color`.

  Everything else from the network is refused: another program or subcommand, `npx x`, `uvx x`, `python -c …`,
  `bash ./x.sh`, paths to executables, variables, `sudo`, and any shell syntax (`;`, `&&`, `|`, `$(…)`,
  redirects). `hermes hive show` applies the same rule against the command that last failed with that error on
  this machine; with no such failure it shows only installs the error names. It also needs 2+
  independent installs (`min_peers`; this install's own reports never count), more yes than no, valid signatures, proof of work (NIP-13, 16 bits by
  default) and content that matches its own hashes. Network fixes are shown as unverified text, never as an
  instruction.

  Every **install** from the network also carries this line: *"this installs code from a public package registry.
  Check that the package name is the one you expect: a private or internal name could be registered publicly by
  someone else."* `hermes hive settings --network-installs off` hides installs from other installs altogether
  (flag fixes still come through). Local fixes and flag fixes carry no such line.

### What the rules cannot rule out

Written down so nobody has to discover it:

- **Dependency confusion.** Shape 1 trusts the package name the error asks for. If your error names a private
  package (`No module named 'acme_internal_utils'`) and someone registers that name on PyPI or npm, a peer can
  suggest installing it, and the public package is what would be installed. Hive cannot tell a private name from a
  public one without asking the registry. Mitigations: the install warning above, `--network-installs off`, and the
  agent still has to choose to run it. If your errors name private packages, turn network installs off.
- **A real package can still be a bad package.** Shape 1 checks that the name is the one the error asks for, not
  what that package does; installing runs the package's own install scripts.
- **Votes and "it did not work" reports are free to fake.** They never let a command through (the shape does), but
  fake "did not work" reports can hide a good fix (a fix is shown only with more yes than no).
- **Lookups reveal which errors you hit.** In `read` mode, relays see hashes of your normalized error lines; a
  common error's hash can be guessed.
- **The flag screens are name lists.** A flag whose name does not say what it does (a tool where `-q` deletes
  something) would pass shape 2. The token-for-token rule keeps the program, subcommand and arguments yours; the
  added switch is the stranger's.

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
- the **error line** gets the same secret screen as the fix steps (API keys such as `sk-…`, `ghp_…`, `xoxb-…`,
  JWTs, long tokens): if it matches, the fix stays private;
- Hermes's own secret redactor gets a final say.

Only fixes in a shape other installs accept are published at all; anything else stays on your machine, and
`hermes hive outbox` says why. `hermes hive outbox` shows exactly what was or would be published. The install key is random and lives in
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
hermes hive settings [--min-peers N] [--min-pow N] [--network-installs on|off]
                              network thresholds (stored with the plugin's data)
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
| Machine B, labelled network text shown (6 runs, final code) | 2, 2, 3, 3, 2, 8 | fixed 6 of 6: fail → "Unverified text from 1 anonymous Nostr peer" → `datactl sync --rebuild-index` → 42 records |
| Earlier demo tool (fix was a separate subcommand), no hint (4 runs) | 5, 7, 8, 10 | 3 fixed, 1 gave up |

The no-hint row was measured before the demo tool's fix became a flag and has not been repeated with it.

`hermes hive learn` on machine A's own past sessions: 80% of its failures were errors it had already fixed before.
Hook cost: under 0.1 ms per terminal command (no network on the hot path; lookups only on a failure, cached).

## Tests

`python tests/test_hive.py`: 41 offline tests, and `python tests/test_attacks.py`: 20 adversarial test groups
(see below). The offline tests include three installs trading a fix over a local
mock relay, poisoned fixes (wrong package, `curl | sh`) vouched for by three fake installs that are refused, two
fake peers offering `npm exec <pkg>` / `go run <mod>@latest` after an npm / go failure (nothing is shown), a table
of fetch-and-run commands for 20+ package managers and build tools, and the exact command sequences the live model
produced (pipes like `| tee` that hide a failure, `&&` chains, full tool paths).

`tests/test_attacks.py` attacks the network rules the way a stranger would: installs that name the right package but
fetch from somewhere else (npm aliases `foo@npm:evil`, `user/repo` and `github:` shorthands, brew taps, other Go
modules with the same last name, paths, tarballs, `-e`/`-r`/`--index-url`/`--registry`/`--git`/`--path`/`--source`),
short options with a glued value (`tar -Ish`, `man -Psh`), flags that throw work away or skip a confirmation
(`git checkout -f`, `git reset --hard`, `terraform apply --auto-approve`, `pulumi up --yes`, `-y`), Windows and
versioned program names (`Git.exe`, `npm.cmd`, `node18`), control, invisible and look-alike characters, shell
syntax in every position, pip installs of an import name instead of its package (`yaml`, `sklearn`, `cv2` …),
`--break-system-packages`, destructive programs (`dropdb`, `kill`, `wipefs`, `userdel`) and `--force` outside
dependency maintenance, the install warning and `--network-installs off` over a relay, a 30-key Sybil swarm over a
relay, and a million-case random mutation run checked against an independent, stricter statement of the rule.
