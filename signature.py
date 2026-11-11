"""Turning a failed command into something two machines can agree on, and a fix into something safe to share.

* ``error_line`` picks the line that says what went wrong (the last Python exception line, ``error:``/``fatal:``
  lines, ``E:`` from apt, ``npm ERR!``, ``command not found`` ...).
* ``normalize`` removes everything that differs between machines or could identify one: paths, user and host
  names, URLs, e-mail and IP addresses, hashes, numbers, long quoted text. What is left is the shape of the error.
* ``signature`` hashes program + shape. Same error on two machines, same signature.
* ``template_step`` prepares a fix command for sharing, and ``shareable`` says whether it may leave the machine.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
from typing import Optional

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ERROR_HINT = re.compile(
    r"(error|exception|fatal|failed|failure|denied|not found|no such|cannot|can't|could not|couldn't|unable|"
    r"refused|invalid|missing|unknown|unrecognized|not installed|not recognized|permission|traceback|panic|"
    r"segmentation fault|killed|timed out|timeout)", re.I)
_PY_EXC = re.compile(r"^\s*(?:[\w.]+\.)?[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Warning)\b[:(]?")
_STRONG = re.compile(r"^\s*(?:(?:error|fatal|E|panic)\b[:!]?|(?:npm\s+)?ERR!)|command not found|"
                     r"No such file or directory|Permission denied|not recognized as", re.I)

_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"<>)]+", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_USERHOST = re.compile(r"\b[\w.-]+@[\w.-]+(?::\S*)?")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_IPV6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.I)
_WINPATH = re.compile(r"\b[A-Za-z]:\\[^\s'\"<>|]*")
_PATH = re.compile(r"(?:(?<=[\s'\"=(:,\[])|^)(?:~|\.{1,2})?(?:/[^\s'\"<>|:,;()\[\]]+)+/?")
_NUM = re.compile(r"(?<![\w<])\d+(?:[.:]\d+)*(?![\w>])")
_LONGQUOTE = re.compile(r"(['\"`])([^'\"`]{61,})\1")
_SPACES = re.compile(r"\s+")

MAX_ERROR = 240


def _clean(text: str) -> str:
    return _ANSI.sub("", text or "").replace("\r", "\n")


def error_line(output: str) -> str:
    """The most telling line of a failed command's output (up to two lines joined for Python tracebacks)."""
    lines = [ln.rstrip() for ln in _clean(output).splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in reversed(lines):  # Python: the exception line is the last one of the traceback
        if _PY_EXC.match(ln):
            return ln.strip()
    for ln in lines:            # tools that print "error:" first and details after
        if _STRONG.search(ln):
            return ln.strip()
    for ln in reversed(lines):
        if _ERROR_HINT.search(ln):
            return ln.strip()
    return lines[-1].strip()


def normalize(text: str) -> str:
    """The machine-independent shape of an error line."""
    s = _clean(text).strip()
    s = _URL.sub("<URL>", s)
    s = _EMAIL.sub("<EMAIL>", s)
    s = _USERHOST.sub("<USER>@<HOST>", s)
    s = _UUID.sub("<ID>", s)
    s = _IPV4.sub("<IP>", s)
    s = _IPV6.sub("<IP>", s)
    s = _WINPATH.sub("<PATH>", s)
    s = _PATH.sub("<PATH>", s)
    s = _HEX.sub("<HEX>", s)
    s = _LONGQUOTE.sub(r"\1<TEXT>\1", s)
    s = _NUM.sub("<N>", s)
    s = re.sub(r"(<PATH>)(?:\s*<PATH>)+", r"\1", s)
    s = _SPACES.sub(" ", s).strip()
    return s[:MAX_ERROR]


_WRAPPERS = {"sudo", "doas", "time", "nice", "nohup", "env", "command", "exec", "stdbuf", "timeout", "xargs"}
_RUNNERS = {"npx", "uvx", "pipx", "bunx", "pnpx"}


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return command.split()


_SETUP = {"cd", "pushd", "popd", "export", "source", ".", "set", "unset", "alias", "true", ":"}


def segments(command: str) -> list[str]:
    """The commands of a list (`a && b; c || d`), split outside quotes; pipes stay inside a segment."""
    out, buf, quote, i, c = [], [], "", 0, command.strip()
    while i < len(c):
        ch = c[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif c.startswith("&&", i) or c.startswith("||", i):
            out.append("".join(buf).strip()); buf = []; i += 1
        elif ch in ";\n":
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf).strip())
    return [x for x in out if x]


def first_segment(command: str) -> str:
    """The first real command of a list, skipping set-up such as `cd dir &&` or `export X=1;`."""
    segs = segments(command) or [command.strip()]
    for seg in segs:
        head = re.split(r"\s*\|\s*", seg, maxsplit=1)[0]
        toks = head.split()
        if toks and os.path.basename(toks[0]) not in _SETUP:
            return head
    return re.split(r"\s*\|\s*", segs[-1], maxsplit=1)[0]


def program(command: str) -> str:
    """The program a command runs: `sudo -E python3 -m pip install x` -> `pip`, `npx tsc` -> `tsc`."""
    toks = _tokens(first_segment(command))
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", t) or t in _WRAPPERS or (t.startswith("-") and i > 0 and toks[0] in _WRAPPERS):
            i += 1
            if t == "timeout" and i < len(toks) and re.fullmatch(r"[\d.]+[smhd]?", toks[i]):
                i += 1
            continue
        break
    if i >= len(toks):
        return ""
    name = os.path.basename(toks[i])
    rest = toks[i + 1:]
    if re.fullmatch(r"python[\d.]*", name) and len(rest) >= 2 and rest[0] == "-m":
        return os.path.basename(rest[1])
    if name in _RUNNERS and rest:
        return os.path.basename(next((r for r in rest if not r.startswith("-")), name))
    if name == "uv" and rest[:1] == ["run"] and len(rest) > 1:
        return os.path.basename(next((r for r in rest[1:] if not r.startswith("-")), "uv"))
    if re.fullmatch(r"pip[\d.]*", name):
        return "pip"
    if re.fullmatch(r"python[\d.]*", name):
        return "python"
    return name


def _strip_prefix(toks: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and wrappers (sudo, env, timeout 30 ...)."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", t) or t in _WRAPPERS or (t.startswith("-") and i > 0 and toks[0] in _WRAPPERS):
            i += 1
            if t == "timeout" and i < len(toks) and re.fullmatch(r"[\d.]+[smhd]?", toks[i]):
                i += 1
            continue
        break
    return toks[i:]


def core_shape(command: str) -> str:
    """What a command really does: set-up dropped, the program by name, machine details removed.
    `cd ~/x && ./bin/datactl sync`, `/opt/t/datactl sync` and `datactl sync` share one core shape."""
    toks = _strip_prefix(_tokens(first_segment(command)))
    if not toks:
        return ""
    toks[0] = os.path.basename(toks[0])
    return normalize(" ".join(toks))


def program_by_name(segment: str, prog: str) -> str:
    """Rewrite a fix step that calls `prog` through a path (`/home/me/bin/datactl rebuild`) to call it by name.
    Only the path text is replaced, so redirections and quoting elsewhere are untouched."""
    rest = _strip_prefix(_tokens(segment))
    if rest and "/" in rest[0] and os.path.basename(rest[0]) == prog:
        for form in (f'"{rest[0]}"', f"'{rest[0]}'", rest[0]):
            if form in segment:
                return segment.replace(form, prog, 1)
    return segment


_NOISE = re.compile(r"\s+(?:\d?>&\d|&>\s*/dev/null|\d?>>?\s*/dev/null)|\s*\|\|\s*(?:true|:)\s*$|"
                    r"\s*\|\s*(?:head|tail|less|more|cat)(?:\s+-?[\w]+)*\s*$")


def canonical_step(step: str) -> str:
    """A fix step without output plumbing: `x 2>&1 | head -20` and `x` are the same fix."""
    prev = None
    s = step.strip()
    while prev != s:
        prev, s = s, _NOISE.sub("", s).strip()
    return s


_SETUP_ERROR = re.compile(r"\b(cd|pushd|popd|source|export|\.)\s*:\s")


_PROG_ERROR = re.compile(r"^\s*[\w./+-]+:\s+(?:error|fatal)\b", re.I)   # "datactl: error: ...", "git: fatal: ..."
_READERS = {"grep", "egrep", "rg", "ag", "ack", "cat", "bat", "tail", "head", "less", "more", "journalctl", "awk",
            "sed", "jq", "yq", "find", "ls", "zcat", "zgrep", "strings", "dmesg", "docker", "kubectl", "git"}


def masked_failure(command: str, output: str) -> bool:
    """`cmd | tee log`, `cmd | head` or `cmd || true` exit 0 even when cmd failed. True when such a command
    printed a clear error line (the same shapes Hermes's own terminal hints look for)."""
    pipeline = bool(re.search(r"(?<!\|)\|(?!\|)", command)) or bool(re.search(r"\|\|\s*(true|:|echo\b)", command))
    if not pipeline or program(command) in _READERS:
        return False                       # `grep ERROR app.log | tail` prints error lines from a file: that is data
    lines = [ln for ln in _clean(output).splitlines() if ln.strip()]
    return any(_PY_EXC.match(ln) or _STRONG.search(ln) or _PROG_ERROR.match(ln) for ln in lines[-20:])


def signature(command: str, output: str) -> tuple[str, str, str]:
    """(signature, program, normalized error) for a failed command."""
    line = error_line(output)
    m = _SETUP_ERROR.search(line)
    prog = m.group(1) if m else program(command)
    # `datactl sync && grep x f` failing in grep says "grep: f: No such file": the error is grep's
    named = re.match(r"\s*([\w.+-]+):\s", line)
    if not m and named:
        progs = {program(p) for seg in segments(command) for p in re.split(r"\s*\|\s*", seg)}
        if named.group(1) in progs:
            prog = named.group(1)
    shape = normalize(line)
    digest = hashlib.sha256(f"{prog}\n{shape}".encode()).hexdigest()[:32]
    return digest, prog, shape


def command_shape(command: str) -> str:
    """A command with machine details removed, for comparing a retry with the original."""
    return normalize(command)


# ── fixes ──────────────────────────────────────────────────────────────────────────────────────────────────────

_PUBLIC_HOSTS = {"pypi.org", "files.pythonhosted.org", "github.com", "raw.githubusercontent.com",
                 "registry.npmjs.org", "npmjs.com", "crates.io", "rubygems.org", "proxy.golang.org", "deb.debian.org",
                 "archive.ubuntu.com", "download.docker.com", "nodejs.org", "astral.sh", "brew.sh"}
_SYSTEM_DIRS = ("/usr/", "/etc/", "/opt/", "/var/", "/bin/", "/sbin/", "/lib", "/tmp/", "/dev/null", "/proc/", "/sys/")
_SECRETISH = re.compile(r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|auth|bearer|private|credential|cookie)"
                        r"\s*[=:]|--password\b|\b(mysql|mysqldump|mysqladmin|sshpass|mongo\w*)\b[^\n]*\s-p\s*\S{3,}|"
                        r"\b(sk|pk|rk|ghp|gho|xox[abp]|AKIA)[-_][A-Za-z0-9]")


def template_step(command: str, home: Optional[str] = None) -> str:
    """A fix command as it may be shown elsewhere: the user's home becomes ~, other private paths <PATH>."""
    home = home or os.path.expanduser("~")
    s = command.strip()
    if home and home not in ("/", ""):
        s = s.replace(home.rstrip("/") + "/", "~/").replace(home.rstrip("/"), "~")
    s = re.sub(r"/(?:home|Users)/[^/\s'\"]+", "~", s)

    def path_sub(m: re.Match) -> str:
        p = m.group(0)
        if p.startswith(_SYSTEM_DIRS) or p in ("/", "/tmp"):
            return p
        if p.startswith("~/."):          # dot-config folders (~/.npmrc, ~/.config/x) are the same everywhere
            return p
        if p.startswith(("~", "./", "../")) and p.count("/") <= 1:
            return p
        return "<PATH>"
    s = re.sub(r"(?:(?<=[\s'\"=])|^)(?:~|\.{1,2})?(?:/[^\s'\"<>|;&]+)+", path_sub, s)
    return _SPACES.sub(" ", s)[:300]


def shareable(step: str) -> tuple[bool, str]:
    """May this (templated) fix step leave the machine? (ok, reason when not)."""
    if "<PATH>" in step:
        return False, "mentions a private path"
    if _SECRETISH.search(step):
        return False, "looks like it carries a secret"
    for host in re.findall(r"[a-z][a-z0-9+.-]*://([^/\s:'\"]+)", step, re.I):
        h = host.lower()
        if not any(h == p or h.endswith("." + p) for p in _PUBLIC_HOSTS):
            return False, f"uses a non-public host ({h})"
    if _IPV4.search(step) or _USERHOST.search(step) or _EMAIL.search(step):
        return False, "names a machine or a person"
    try:
        from agent.redact import redact_for_egress  # Hermes's own secret patterns (private module, guarded)
        if redact_for_egress(step) != step:
            return False, "Hermes's redactor found a secret"
    except Exception:  # noqa: BLE001 - outside Hermes or a changed API: our own patterns above still apply
        pass
    return True, ""


def fix_id(steps: list[str]) -> str:
    return hashlib.sha256("\n".join(steps).encode()).hexdigest()[:16]
