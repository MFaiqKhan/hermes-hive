"""What Hive will never show, what it shows with a warning, and when a shared install is believable.

Hive only ever shows a fix as text; it never runs anything. Still, a hint is a suggestion the agent may follow,
so a fix that could hurt is not shown at all (BLOCK), and a risky one carries a warning (WARN). Fixes that came
from other installs get two more rules: no downloads from non-public hosts, and a package install must be
explained by the error itself (a "ModuleNotFoundError: httpx" justifies `pip install httpx`, not `pip install
reqeusts`), unless many independent installs vouch for it.
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
# Programs whose arguments alone can run code, reach other machines or destroy files: a stranger's "same program,
# other flags" is never accepted for them.
_POWERFUL = {"ssh", "scp", "sftp", "rsync", "find", "awk", "gawk", "mawk", "nawk", "sed", "make", "cmake",
             "docker", "podman", "kubectl", "helm", "systemctl", "crontab", "rm", "rmdir", "mv", "cp", "dd", "ln",
             "chmod", "chown", "chgrp", "truncate", "shred", "mount", "umount", "tee", "install", "at",
             "nc", "ncat", "socat", "telnet", "ftp", "tftp", "scutil", "launchctl", "reg", "schtasks", "gdb", "strace"}
# Arguments that make an ordinary program run something else or talk to a chosen server.
_EXEC_ARG = re.compile(r"(?i)(?:^|\s)--?(?:exec(?:dir)?|eval|command|to-command|rsh|upload-pack|receive-pack|"
                       r"entrypoint|checkpoint-action|use-compress-program|run|shell|hook|plugin|preload|script)"
                       r"(?:[=\s]|$)|proxycommand|localcommand|sshcommand|\bcore\.|\balias\.|!|://|"
                       r"\bLD_PRELOAD\b|\bPYTHONPATH\b|\bNODE_OPTIONS\b")


def network_step_allowed(step: str, error: str, failed_program: str) -> bool:
    """True only for the two shapes a shared fix may take:
    1. a known installer installing packages named in the error (`pip install httpx` for "No module named httpx");
    2. the program that failed, run again with other arguments (`datactl cache rebuild --force` after
       `datactl sync` failed), when that program is not an interpreter, shell or package runner."""
    if not step or _SHELL_SYNTAX.search(step):
        return False
    try:
        toks = shlex.split(step)
    except ValueError:
        return False
    if not toks or "/" in toks[0] or re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
        return False
    if install_targets(step):
        return justified_by_error(step, error) and not _PKG_URL.search(step)
    head = toks[0]
    from .signature import program as _program
    prog = _program(step)
    if not failed_program or prog != failed_program or head != prog:
        return False
    if prog in _RUNNERS or prog in _POWERFUL or _EXEC_ARG.search(step):
        return False
    if prog == "git" and re.search(r"(?:^|\s)(?:-c|config|submodule\s+foreach|--config-env)(?:\s|=|$)", step):
        return False                     # `git -c core.x=...` / `git config` can make git run anything
    return True
