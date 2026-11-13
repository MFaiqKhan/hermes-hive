"""What Hive will never show, what it shows with a warning, and when a shared install is believable.

Hive only ever shows a fix as text; it never runs anything. Still, a hint is a suggestion the agent may follow,
so a fix that could hurt is not shown at all (BLOCK), and a risky one carries a warning (WARN). Fixes that came
from other installs are anonymous text, and votes for them cost nothing to fake. So however many installs
report one, it is shown only in one of two fixed shapes (`network_step_allowed`):

1. a known installer installing packages the error itself names (a "ModuleNotFoundError: httpx" justifies
   `pip install httpx`, not `pip install reqeusts`), never from a URL, file or other index;
2. the exact command that failed on this machine, with plain flags added at the end (`npm install` ->
   `npm install --legacy-peer-deps`). A stranger can never choose the program, the subcommand or any argument.
"""
from __future__ import annotations

import re
import shlex

BLOCK = [
    (r"\b(curl|wget|fetch|iwr|invoke-webrequest)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", "pipes a download into a shell"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(python\d?|perl|ruby|node)\b", "pipes a download into an interpreter"),
    (r"\b(sh|bash|zsh)\s+(-c\s+)?[\"']?\$\((curl|wget)", "runs a downloaded script"),
    (r"base64\s+(-d|--decode)[^|]*\|\s*(ba|z)?sh", "runs encoded code"),
    (r"\beval\s+[\"']?\$\(", "evaluates generated code"),
    (r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~/?|\$HOME/?|\*|/\*|\.\.?/?)(\s|$)", "deletes a whole directory tree"),
    (r"\bsudo\s+rm\b", "deletes files as root"),
    (r"\bmkfs(\.\w+)?\b|\bdd\b[^\n]*\bof=/dev/|>\s*/dev/(sd|nvme|hd|disk)", "writes to a disk device"),
    (r"\b(chmod|chown)\s+-R\b[^\n]*\s(/|~|\$HOME)(\s|$)", "changes permissions of everything"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\b(shutdown|reboot|halt|poweroff)\b|\binit\s+[06]\b", "shuts the machine down"),
    (r"\bcrontab\s+-r\b|\buserdel\b|\busermod\b", "changes system accounts or schedules"),
    (r"(>>?|\btee\b(\s+-a)?|\bsed\s+-i\S*\b[^\n]*)\s*/etc/", "writes to system configuration under /etc"),
    (r"\.ssh/", "touches SSH keys"),
    (r"/dev/tcp/|\bnc\b[^\n]*\s-e\s|\bncat\b[^\n]*--exec|\bsocat\b[^\n]*exec", "opens a remote shell"),
    (r"\biptables\s+-F\b|\bufw\s+disable\b|\bsetenforce\s+0\b", "turns off a firewall or SELinux"),
    (r"\bhistory\s+-c\b|unset\s+HISTFILE", "hides shell history"),
    (r"\bgit\s+push\b[^\n]*(--force|-f\b)", "force-pushes"),
    (r"--trusted-host\b", "turns off package TLS checks"),
]
WARN = [
    (r"\bsudo\b|\bdoas\b", "runs as root"),
    (r"\b(npm|pnpm|yarn)\s+(i|install|add)\b[^\n]*\s(-g|--global)\b", "installs a global package"),
    (r"--break-system-packages", "installs into the system Python"),
    (r"\brm\s+-[a-zA-Z]*[rf]", "deletes files"),
    (r"\bchmod\s+[0-7]*7[0-7]{0,2}7\b|\bchmod\s+(a|o)\+w\b", "makes files writable by everyone"),
    (r"--privileged\b", "runs a privileged container"),
    (r"\b(kill|pkill|killall)\b", "stops processes"),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "stops a service"),
    (r">>?\s*~?/?[^\s]*\.(bashrc|zshrc|profile|bash_profile|config/fish/config\.fish)\b", "edits the shell start-up file"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", "throws away local changes"),
    (r"\b(pip|npm|apt|apt-get|brew|cargo|gem)\s+(uninstall|remove|purge)\b", "removes software"),
]
_BLOCK = [(re.compile(p, re.I), why) for p, why in BLOCK]
_WARN = [(re.compile(p, re.I), why) for p, why in WARN]
_PKG_URL = re.compile(r"\b(pip\d?|uv\s+pip|pipx|npm|pnpm|yarn|gem|cargo|go)\s+(install|add|get)\b[^\n]*"
                      r"(git\+|https?://|\.whl\b|\.tar\.gz\b|\.tgz\b|file:)", re.I)
_INDEX = re.compile(r"--(extra-)?index-url\s+(\S+)|--registry[=\s]+(\S+)", re.I)


def screen(step: str, origin: str = "local") -> tuple[str, list[str]]:
    """("ok" | "warn" | "block", reasons) for one fix step."""
    reasons = [why for rx, why in _BLOCK if rx.search(step)]
    if origin != "local":
        if _PKG_URL.search(step):
            reasons.append("installs a package from a URL or file")
        for m in _INDEX.finditer(step):
            url = (m.group(2) or m.group(3) or "").lower()
            if not re.match(r"https://(pypi\.org|registry\.npmjs\.org)(/|$)", url):
                reasons.append("uses a non-default package index")
    if reasons:
        return "block", reasons
    warns = [why for rx, why in _WARN if rx.search(step)]
    return ("warn" if warns else "ok"), warns


# ── is a shared install explained by the error? ───────────────────────────────────────────────────────────────

_INSTALLERS = {
    ("pip", "install"), ("pip3", "install"), ("uv", "add"), ("pipx", "install"), ("poetry", "add"),
    ("npm", "install"), ("npm", "i"), ("npm", "add"), ("pnpm", "add"), ("pnpm", "install"), ("yarn", "add"),
    ("apt", "install"), ("apt-get", "install"), ("dnf", "install"), ("yum", "install"), ("pacman", "-S"),
    ("brew", "install"), ("cargo", "install"), ("cargo", "add"), ("gem", "install"), ("go", "install"),
    ("go", "get"), ("conda", "install"), ("choco", "install"), ("winget", "install"),
}


def install_targets(step: str) -> list[str]:
    """Package names a step installs (empty when it is not an install)."""
    try:
        toks = shlex.split(step)
    except ValueError:
        toks = step.split()
    toks = [t for t in toks if t not in ("sudo", "doas")]
    if len(toks) >= 3 and re.fullmatch(r"python[\d.]*", toks[0]) and toks[1] == "-m":
        toks = toks[2:]
    if toks[:2] == ["uv", "pip"]:
        toks = toks[1:]
    if len(toks) < 3 or (toks[0], toks[1]) not in _INSTALLERS:
        return []
    names = []
    for t in toks[2:]:
        if t.startswith("-") or t in ("install", "add"):
            continue
        name = re.split(r"[<>=!~;\[@ ]", t, maxsplit=1)[0]
        if name.startswith("@") and "/" in t:     # npm scoped package: @scope/name@version
            name = "@" + t[1:].split("@")[0]
        if name and not name.startswith("<"):
            names.append(name.lower())
    return names


def _norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "", name.lower()).split("/")[-1]


# Python import names whose package is called something else (the usual "No module named X" fixes).
_IMPORT_TO_PACKAGE = {
    "yaml": "pyyaml", "cv2": "opencvpython", "pil": "pillow", "sklearn": "scikitlearn", "bs4": "beautifulsoup4",
    "dateutil": "pythondateutil", "dotenv": "pythondotenv", "jwt": "pyjwt", "magic": "pythonmagic",
    "serial": "pyserial", "usb": "pyusb", "crypto": "pycryptodome", "fitz": "pymupdf", "docx": "pythondocx",
    "pptx": "pythonpptx", "git": "gitpython", "telegram": "pythontelegrambot", "googleapiclient": "googleapipythonclient",
    "attr": "attrs", "zmq": "pyzmq", "ldap": "pythonldap", "mysqldb": "mysqlclient", "psycopg2": "psycopg2binary",
}


def _error_words(error: str) -> set[str]:
    """Normalized words of an error line; dotted names also count by each part (`yaml.constructor` -> yaml)."""
    words = set()
    for w in re.findall(r"[A-Za-z0-9@][A-Za-z0-9._/@-]*", error):
        for part in [w] + re.split(r"[./]", w):
            n = _norm_pkg(part)
            if n:
                words.add(n)
    return words


def justified_by_error(step: str, error: str) -> bool:
    """True when every package a step installs is named in the error as a whole word (or is the known package
    for an import named there), or the step installs nothing. `colorama` in the error does not justify `color`."""
    names = install_targets(step)
    if not names:
        return True
    words = _error_words(error)
    mapped = {pkg for imp, pkg in _IMPORT_TO_PACKAGE.items() if imp in words}
    return all(_norm_pkg(n) and (_norm_pkg(n) in words or _norm_pkg(n) in mapped) for n in names)


# ── what a fix from the network may look like ────────────────────────────────────────────────────────────────
# Anything an anonymous peer could use to run its own code is out, whatever the votes: interpreters and package
# runners (python -c, node -e, npx x, uvx x, bash ./x.sh ...), and any shell syntax beyond one plain command.
_RUNNERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "lua", "bash", "sh", "zsh", "fish",
            "dash", "ksh", "pwsh", "powershell", "npx", "pnpx", "bunx", "uvx", "pipx", "uv", "curl", "wget", "eval",
            "exec", "source", ".", "sudo", "doas", "env", "xargs", "nohup", "osascript", "cmd"}
_SHELL_SYNTAX = re.compile(r"[;&|`<>]|\$\(|\$\{|\n")
# Programs whose arguments alone can run code, reach other machines or destroy files: never accepted from a
# stranger, not even as "your own command plus a flag".
_POWERFUL = {"ssh", "scp", "sftp", "rsync", "find", "awk", "gawk", "mawk", "nawk", "sed", "make", "cmake",
             "docker", "podman", "kubectl", "helm", "systemctl", "crontab", "rm", "rmdir", "mv", "cp", "dd", "ln",
             "chmod", "chown", "chgrp", "truncate", "shred", "mount", "umount", "tee", "install", "at",
             "nc", "ncat", "socat", "telnet", "ftp", "tftp", "scutil", "launchctl", "reg", "schtasks", "gdb", "strace"}
# Arguments that make an ordinary program run something else or talk to a chosen server.
_EXEC_ARG = re.compile(r"(?i)(?:^|\s)--?(?:exec(?:dir)?|eval|command|to-command|rsh|upload-pack|receive-pack|"
                       r"entrypoint|checkpoint-action|use-compress-program|run|shell|hook|plugin|preload|script)"
                       r"(?:[=\s]|$)|proxycommand|localcommand|sshcommand|\bcore\.|\balias\.|!|://|"
                       r"\bLD_PRELOAD\b|\bPYTHONPATH\b|\bNODE_OPTIONS\b")

# A flag a stranger may add: a plain switch (`--force`, `--legacy-peer-deps`, `-y`, `-fdx`), or a long switch with
# a number (`--jobs=1`, `--retries=3`). Nothing that could NAME something (a package, file, host, URL, statement:
# `--package=evil`, `--registry=evil.example`, `--config=x.toml`, `--strict-ssl=false`), and no long word glued to
# a single dash (`mysql -eshutdown`, `psql -cvacuum`).
_PLAIN_FLAG = re.compile(r"--[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?:=\d+(?:\.\d+)*)?|-[A-Za-z0-9]{1,3}")
# An extra screen on top of that shape, not the guarantee: switches whose name says they turn off a transport,
# signature, sandbox or permission check, or let more code run (`--insecure`, `--no-verify`, `--nogpgcheck`,
# `--gpgcheck=0`, `--no-sandbox`, `--allow-root`, `--unsafe-perm`, `--dangerously-allow-all-builds`).
_WEAKENS = re.compile(r"(?i)ssl|tls|cert|verif|gpg|sign|sandbox|checksum|integrity|hash|secure|trust|auth|root|"
                      r"priv|unsafe|allow|danger|script|downgrade|force-yes|^-k$")
# Subcommands that fetch or run code (the list from the catalog review, plus aliases and close relatives): never
# accepted as the subcommand of a network step, whatever the program.
_FETCH_RUN = {"exec", "x", "dlx", "create", "init", "run", "run-script", "start", "test", "download", "wheel",
              "stop", "restart", "t", "tst", "it", "cit", "install-test", "install-ci-test", "rum", "urn", "generate",
              "tool", "bench", "script", "serve", "dev", "watch", "eval", "repl", "shell", "console", "publish"}
# Package managers and build tools run code under many names (`cargo build` runs build scripts, `mvn exec:java`),
# so for them only a short list of dependency-maintenance subcommands may be repeated with a new flag.
_PKG_TOOLS = {"npm", "pnpm", "yarn", "bun", "corepack", "pip", "pip3", "pipenv", "poetry", "pdm", "hatch", "conda",
              "mamba", "micromamba", "go", "cargo", "rustup", "gem", "bundle", "bundler", "composer", "dotnet",
              "nuget", "mvn", "mvnw", "gradle", "gradlew", "ant", "sbt", "lein", "mix", "stack", "cabal", "swift",
              "flutter", "dart", "pod", "brew", "apt", "apt-get", "dnf", "yum", "pacman", "zypper", "apk", "snap",
              "flatpak", "nix", "nix-env", "guix", "choco", "winget", "scoop", "just", "task", "rake", "ninja",
              "bazel", "buck", "nx", "turbo", "lerna", "tox", "nox", "invoke", "meson", "scons"}
_MAINTENANCE = {"install", "i", "ci", "update", "upgrade", "up", "dedupe", "prune", "audit", "outdated", "cache",
                "fetch", "why", "list", "ls", "mod", "tidy", "sync", "lock", "restore", "vendor", "clean"}
# yarn, pnpm and bun run a project script for any word that is not one of their own commands (`yarn build`,
# `pnpm clean`), so for them the list is only words that are always the manager's own command.
_SCRIPT_BY_ANY_NAME = {"yarn", "pnpm", "bun", "corepack"}
_ALWAYS_BUILTIN = {"install", "i", "update", "upgrade", "up", "dedupe", "prune", "audit", "outdated", "cache",
                   "fetch", "why", "list", "ls"}


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _install_shape(step: str, toks: list[str], error: str) -> bool:
    """Shape 1: a known installer installing only packages the error names, with nothing but plain flags."""
    if not justified_by_error(step, error) or _PKG_URL.search(step):
        return False
    flags = [t for t in toks if t.startswith("-")]
    return all(_PLAIN_FLAG.fullmatch(t) and not _WEAKENS.search(t) for t in flags)


def _own_command_plus_flags(toks: list[str], failed_command: str) -> bool:
    """Shape 2: the tokens of the command that failed here, unchanged and in order, then only plain flags.

    Nothing is inserted, removed or reordered, so a stranger cannot choose the program, the subcommand or any
    argument, and cannot turn one of the user's own words into a flag's value or the other way round."""
    from .signature import _strip_prefix, program as _program
    own = _strip_prefix(_split(failed_command))
    prog = _program(failed_command) if own else ""
    if not own or not prog or own[0] != prog or toks[:len(own)] != own:
        return False
    if prog in _RUNNERS or prog in _POWERFUL:
        return False
    added = toks[len(own):]
    if not all(_PLAIN_FLAG.fullmatch(t) and not _WEAKENS.search(t) for t in added):
        return False
    if "--" in own or "-" in own:
        return False                     # after a bare `--` an added "flag" would be an argument
    words = [t.lower() for t in own[1:] if not t.startswith("-")]
    sub = words[0] if words else ""
    if any(w in _FETCH_RUN for w in words):
        return False                     # `npm exec x --yes`, `go -C dir run . -v`: not from a stranger
    if prog in _PKG_TOOLS and sub not in (_ALWAYS_BUILTIN if prog in _SCRIPT_BY_ANY_NAME else _MAINTENANCE):
        return False                     # `yarn build`, `cargo build`, `mvn exec:java`: they run code
    return True


def network_step_allowed(step: str, error: str, failed_command: str = "") -> bool:
    """True only for the two shapes a fix from another install may take, however many installs report it:

    1. a known installer installing packages named in the error (`pip install httpx` for "No module named httpx");
    2. the command that failed on this machine, token for token, with plain flags added at the end
       (`npm install --legacy-peer-deps` after `npm install` failed; `datactl sync --rebuild-index` after
       `datactl sync` failed). Never for interpreters, shells, runners or tools whose flags alone can do harm,
       never for fetch-and-run subcommands (`exec`, `run`, `dlx`, `create` ...), and for package managers and
       build tools only for dependency-maintenance subcommands (`install`, `update`, `ci`, `cache` ...).

    `failed_command` is the local failure as peers would write it (see `engine.network_shape`). Without it (no
    such failure recorded on this machine) only shape 1 can pass."""
    if not step or _SHELL_SYNTAX.search(step):
        return False
    toks = _split(step)
    if not toks or "/" in toks[0] or re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
        return False
    if install_targets(step):
        return _install_shape(step, toks, error)
    if _EXEC_ARG.search(step):
        return False
    if toks[0] == "git" and re.search(r"(?:^|\s)(?:-c|config|submodule\s+foreach|--config-env)(?:\s|=|$)", step):
        return False                     # `git -c core.x=...` / `git config` can make git run anything
    return bool(failed_command) and _own_command_plus_flags(toks, failed_command)
