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


# ── program names ─────────────────────────────────────────────────────────────────────────────────────────────

def base_name(prog: str) -> str:
    """The program a name stands for, for every rule below: `NPM.CMD` -> npm, `python3.12` -> python,
    `node18` -> node, `Git.exe` -> git. Rules keyed by program never miss a Windows or versioned spelling."""
    name = (prog or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    name = re.sub(r"\.(exe|cmd|bat|ps1|com)$", "", name)
    stripped = re.sub(r"[\d.]+$", "", name)
    return stripped or name


# Characters that make shown text differ from what a terminal runs, or redraw the screen: control characters
# (carriage return, escape, NUL, backspace ...), invisible format characters (zero-width, bidi overrides, BOM) and
# any space other than the plain one. Text from the network carrying any of them is never stored or shown.
_HIDDEN = re.compile("[\x00-\x1f\x7f-\x9f\u00a0\u1680\u180e\u2000-\u200f\u2028-\u202f\u205f-\u206f\u3000\ufeff\ufff9-\ufffb]")


def has_hidden_chars(text: str) -> bool:
    return bool(_HIDDEN.search(text or ""))


# ── is a shared install explained by the error? ───────────────────────────────────────────────────────────────

_INSTALLERS = {
    ("pip", "install"), ("uv", "add"), ("pipx", "install"), ("poetry", "add"),
    ("npm", "install"), ("npm", "i"), ("npm", "add"), ("pnpm", "add"), ("pnpm", "install"), ("yarn", "add"),
    ("apt", "install"), ("apt-get", "install"), ("dnf", "install"), ("yum", "install"), ("pacman", "-S"),
    ("brew", "install"), ("cargo", "install"), ("cargo", "add"), ("gem", "install"), ("go", "install"),
    ("go", "get"), ("conda", "install"), ("choco", "install"), ("winget", "install"),
}


def _installer_tokens(step: str) -> list[str]:
    """The step's tokens from the installer on (`sudo python3 -m pip install x` -> pip install x), or []."""
    try:
        toks = shlex.split(step)
    except ValueError:
        toks = step.split()
    toks = [t for t in toks if t not in ("sudo", "doas")]
    if len(toks) >= 3 and base_name(toks[0]) == "python" and toks[1] == "-m":
        toks = toks[2:]
    if len(toks) >= 2 and base_name(toks[0]) == "uv" and toks[1] == "pip":
        toks = toks[1:]
    if len(toks) < 3 or (base_name(toks[0]), toks[1]) not in _INSTALLERS:
        return []
    return [base_name(toks[0])] + toks[1:]


def install_targets(step: str) -> list[str]:
    """Package names a step installs (empty when it is not an install)."""
    toks = _installer_tokens(step)
    names = []
    for t in toks[2:]:
        if t.startswith("-") or t in ("install", "add"):
            continue
        if t.startswith("@") and "/" in t:         # npm scoped package: @scope/name@version
            name = "@" + t[1:].split("@")[0]
        else:
            name = re.split(r"[<>=!~;\[@ ]", t, maxsplit=1)[0]
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
    "skimage": "scikitimage", "openssl": "pyopenssl", "win32api": "pywin32", "win32con": "pywin32", "gi": "pygobject",
    "dns": "dnspython", "nacl": "pynacl", "jose": "pythonjose", "multipart": "pythonmultipart",
    "socketio": "pythonsocketio", "slugify": "pythonslugify", "kafka": "kafkapython",
    "mpl_toolkits": "matplotlib", "markdown_it": "markdownitpy",
}
# Import names that are also the real package. Every other key above is refused as a pip name from the network:
# `pip install yaml` for "No module named 'yaml'" installs whatever is registered as `yaml`, not PyYAML, and
# such import-name packages are a known squatting target.
_IMPORT_IS_PACKAGE = {"psycopg2"}
_IMPORT_ONLY = {_norm_pkg(k) for k in _IMPORT_TO_PACKAGE} - _IMPORT_IS_PACKAGE


def _error_words(error: str) -> set[str]:
    """Normalized words of an error line; dotted names also count by each part (`yaml.constructor` -> yaml)."""
    words = set()
    for w in re.findall(r"[A-Za-z0-9@][A-Za-z0-9._/@-]*", error):
        for part in [w] + re.split(r"[./]", w):
            n = _norm_pkg(part)
            if n:
                words.add(n)
    return words


def _error_tokens(error: str) -> set[str]:
    """The error's words exactly as written (case aside), for names that must appear verbatim."""
    return {t.strip(".,:;").lower() for t in re.split(r"[\s'\"`()\[\]{}<>]+", error or "") if t.strip(".,:;")}


# What a package name may look like in a fix from the network. A name is a registry name and nothing else: no
# alias (`foo@npm:evil`), no git host or shorthand (`user/repo`, `github:x/y`), no path, file, tarball, URL or
# protocol (`./foo`, `file:`, `link:`, `workspace:`, `portal:`). Versions are plain version text.
_VER = r"[A-Za-z0-9.*+^~_-]+"
_PY_SPEC = r"(?:===|==|>=|<=|~=|!=|>|<)[A-Za-z0-9.*+!_-]+"
_NAME_SHAPES = {
    "npm": re.compile(rf"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:@{_VER})?"),
    "pip": re.compile(rf"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?(?:{_PY_SPEC}(?:,{_PY_SPEC})*)?"),
    "go": re.compile(rf"[a-z0-9][a-z0-9.-]*(?:/[A-Za-z0-9._~-]+)+(?:@{_VER})?"),
    "brew": re.compile(r"[a-z0-9][a-z0-9@._+-]*"),
    "other": re.compile(rf"[A-Za-z0-9][A-Za-z0-9._+-]*(?:[=@]{_VER})?"),
}
_FAMILY = {"npm": "npm", "pnpm": "npm", "yarn": "npm", "pip": "pip", "pipx": "pip", "uv": "pip", "poetry": "pip",
           "go": "go", "brew": "brew"}
# Flags an install from the network may carry: switches that do not choose where code comes from or where it
# goes. Anything else (an index, registry, source, path, git URL, target, prefix, config, workspace, channel,
# requirements or constraints file, editable install ...) is refused, including every short option not listed.
_INSTALL_FLAGS = {
    "--upgrade", "--user", "--pre", "--no-deps", "--no-cache-dir", "--force-reinstall", "--quiet", "--verbose",
    "--save", "--save-dev", "--save-exact", "--save-optional", "--save-prod", "--save-peer", "--no-save",
    "--legacy-peer-deps", "--force", "--global", "--ignore-scripts", "--no-audit", "--no-fund", "--dev", "--exact",
    "--optional", "--peer", "--locked", "--frozen", "--offline", "--yes", "--assume-yes",
    "--no-install-recommends", "--fix-missing", "--fix-broken", "--reinstall", "--only-upgrade", "--needed",
    "--cask", "--formula", "--build-from-source",
    "-y", "-q", "-v", "-U", "-D", "-E", "-S", "-g", "-P", "-O", "-B",
}


def _name_of(token: str, family: str) -> str:
    """The bare package name of a validated token: version, extras and specifiers dropped."""
    if family == "npm" and token.startswith("@"):
        return "@" + token[1:].split("@", 1)[0]
    if family == "go":
        return token.split("@", 1)[0]
    return re.split(r"[<>=!~\[@]", token, maxsplit=1)[0]


def _named_by_error(name: str, error: str, family: str = "other") -> bool:
    """A plain name must be a whole word of the error (or the known package for an import named there). A name
    with a scope or a path (`@acme/foo`, `github.com/acme/foo`, a tap) must appear in the error exactly, so
    `attacker/foo` or `evil.example/foo` never pass because the error mentions `foo`."""
    if "/" in name:
        return name.lower() in _error_tokens(error)
    words = _error_words(error)
    mapped = {pkg for imp, pkg in _IMPORT_TO_PACKAGE.items() if _norm_pkg(imp) in words}
    n = _norm_pkg(name)
    if family == "pip" and n in _IMPORT_ONLY:
        return False                     # `pip install sklearn`: the import name, not scikit-learn
    return bool(n) and (n in words or n in mapped)


def justified_by_error(step: str, error: str) -> bool:
    """True when every package a step installs is named in the error as a whole word (or is the known package
    for an import named there), or the step installs nothing. `colorama` in the error does not justify `color`."""
    toks = _installer_tokens(step)
    if not toks:
        return True
    family = _FAMILY.get(toks[0], "other")
    names = [t for t in toks[2:] if not t.startswith("-")]
    return bool(names) and all(_NAME_SHAPES[family].fullmatch(t) and _named_by_error(_name_of(t, family), error, family)
                               for t in names)


# ── what a fix from the network may look like ────────────────────────────────────────────────────────────────
# Anything an anonymous peer could use to run its own code is out, whatever the votes: interpreters and package
# runners (python -c, node -e, npx x, uvx x, bash ./x.sh ...), and any shell syntax beyond one plain command.
_RUNNERS = {"python", "node", "nodejs", "deno", "bun", "ruby", "perl", "php", "lua", "luajit", "bash", "sh", "zsh",
            "fish", "dash", "ksh", "csh", "tcsh", "pwsh", "powershell", "npx", "pnpx", "bunx", "uvx", "pipx", "uv",
            "curl", "wget", "eval", "exec", "source", ".", "sudo", "doas", "env", "xargs", "nohup", "osascript",
            "cmd", "java", "dotnet-script", "tsx", "ts-node", "rscript", "julia", "groovy", "scala", "kotlin",
            "elixir", "erl", "swift"}
_SHELL_SYNTAX = re.compile(r"[;&|`<>]|\$\(|\$\{|\n")
# Programs whose arguments alone can run code, reach other machines, destroy files or change remote
# infrastructure: never accepted from a stranger, not even as "your own command plus a flag".
_POWERFUL = {"ssh", "scp", "sftp", "rsync", "find", "awk", "gawk", "mawk", "nawk", "sed", "make", "cmake",
             "docker", "podman", "kubectl", "helm", "systemctl", "crontab", "rm", "rmdir", "mv", "cp", "dd", "ln",
             "chmod", "chown", "chgrp", "truncate", "shred", "mount", "umount", "tee", "install", "at",
             "nc", "ncat", "socat", "telnet", "ftp", "tftp", "scutil", "launchctl", "reg", "schtasks", "gdb", "strace",
             # archivers: many options run a program (tar -I/-F/--to-command, zip -TT)
             "tar", "bsdtar", "gtar", "zip", "unzip", "7z", "7za", "rar", "unrar", "cpio",
             # pagers and editors can run shell commands or load scripts from the folder
             "man", "less", "more", "most", "vi", "vim", "nvim", "view", "emacs", "nano", "ed", "ex",
             # database clients run statements
             "psql", "mysql", "mariadb", "mongo", "mongosh", "redis-cli", "sqlite", "sqlcmd", "clickhouse-client",
             # cloud, deploy and infrastructure tools change remote systems
             "terraform", "tofu", "terragrunt", "pulumi", "gcloud", "gsutil", "bq", "aws", "az", "ansible",
             "ansible-playbook", "flyctl", "fly", "heroku", "vercel", "netlify", "firebase", "wrangler", "gh", "glab",
             "doctl", "oc", "eksctl", "serverless", "sls", "cdk", "sam", "packer", "vagrant", "nomad", "consul",
             "vault", "kustomize", "skaffold", "tilt", "railway", "supabase",
             # programs that delete data, stop processes or change disks, users or firewalls (disk formatting,
             # shutdown and account removal are also in BLOCK, which applies to every hint)
             "dropdb", "dropuser", "pg_restore", "pg_ctl", "mysqladmin", "mongorestore", "wipefs", "fdisk", "fsck",
             "sfdisk", "parted", "gdisk", "kill", "pkill", "killall", "shutdown", "reboot", "poweroff", "halt",
             "iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "userdel", "deluser", "groupdel", "usermod",
             "passwd", "swapoff", "losetup", "cryptsetup", "zfs", "zpool", "lvremove", "vgremove", "pvremove",
             "btrfs", "diskutil", "format", "del", "erase", "rd", "srm", "unlink"}
# Arguments that make an ordinary program run something else or talk to a chosen server.
_EXEC_ARG = re.compile(r"(?i)(?:^|\s)--?(?:exec(?:dir)?|eval|command|to-command|rsh|upload-pack|receive-pack|"
                       r"entrypoint|checkpoint-action|use-compress-program|run|shell|hook|plugin|preload|script)"
                       r"(?:[=\s]|$)|proxycommand|localcommand|sshcommand|\bcore\.|\balias\.|!|://|"
                       r"\bLD_PRELOAD\b|\bPYTHONPATH\b|\bNODE_OPTIONS\b")

# A flag a stranger may add to the user's own command: a single-letter switch (`-v`) or a lowercase long switch
# (`--force`, `--legacy-peer-deps`), optionally with a small number (`--jobs=1`, `--timeout=2.5`). Never a value
# that could name something (`--registry=evil.example`, `--config=x.toml`, `--strict-ssl=false`, `--x=1.2.3.4`),
# never a short option with text glued to it (`tar -Ish`, `man -Psh`, `mysql -eshutdown`).
_PLAIN_FLAG = re.compile(r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:=\d+(?:\.\d+)?)?|-[A-Za-z0-9]")
# Words in a switch's name that turn off a transport, signature, sandbox or permission check, change who owns or
# may write files, let more code run, or open a debugger. Matched against the dash-separated parts of the name, so
# `--unshallow` is not mistaken for `allow`.
_WEAK_WORDS = {"ssl", "tls", "cert", "certs", "certificate", "certificates", "insecure", "k", "verify", "noverify",
               "verification", "gpg", "gpgcheck", "nogpgcheck", "sign", "signed", "signature", "signatures",
               "unsigned", "sandbox", "checksum", "checksums", "integrity", "hash", "hashes", "secure", "trust",
               "trusted", "untrusted", "auth", "unauthenticated", "root", "privileged", "unsafe", "allow", "allowed",
               "danger", "dangerous", "dangerously", "script", "scripts", "downgrade", "downgrades", "perm", "perms",
               "permission", "permissions", "umask", "uid", "gid", "mode", "chmod", "chown", "owner", "setuid",
               "inspect", "inspector", "debug", "debugger", "remote", "listen", "bind", "expose", "public", "break",
               "bypass", "unprotected", "unrestricted"}
# Words for switches that throw work away or skip a confirmation that guards something destructive.
_DESTRUCTIVE_WORDS = {"hard", "purge", "wipe", "destroy", "delete", "drop", "prune", "overwrite", "clobber",
                      "truncate", "recursive", "erase", "nuke", "discard", "yes", "approve", "noconfirm", "mirror",
                      "lease", "reset", "remove", "rm", "y"}
_SKIP_WORDS = {"no", "skip", "without", "disable", "ignore"}
_GIT_SHORT_DESTRUCTIVE = {"-f", "-D", "-d", "-x", "-X", "-r", "-R", "-y"}


def _flag_refused(flag: str, prog: str) -> bool:
    """True for a plain flag that a stranger may still not add: it weakens a check, destroys, or skips a prompt."""
    name = flag.lstrip("-").split("=", 1)[0].lower()
    parts = set(name.split("-"))
    if parts & _WEAK_WORDS or parts & _DESTRUCTIVE_WORDS:
        return True
    if parts & {"confirm", "confirmation", "preview", "prompt", "interactive", "verify", "check", "checks"} \
            and parts & _SKIP_WORDS:
        return True
    if prog == "git" and (flag in _GIT_SHORT_DESTRUCTIVE or "force" in parts):
        return True
    if prog not in _PKG_TOOLS and ("force" in parts or flag == "-f"):
        return True                      # `--force` outside dependency maintenance overrides the program's own guard
    return False


# Subcommands that fetch or run code (the list from the catalog review, plus aliases and close relatives): never
# accepted in a network step, whatever the program.
_FETCH_RUN = {"exec", "x", "dlx", "create", "init", "run", "run-script", "start", "test", "download", "wheel",
              "stop", "restart", "t", "tst", "it", "cit", "install-test", "install-ci-test", "rum", "urn", "generate",
              "tool", "bench", "script", "serve", "dev", "watch", "eval", "repl", "shell", "console", "publish"}
# Package managers and build tools run code under many names (`cargo build` runs build scripts, `mvn exec:java`),
# so for them only a short list of dependency-maintenance subcommands may be repeated with a new flag.
_PKG_TOOLS = {"npm", "pnpm", "yarn", "bun", "corepack", "pip", "pipenv", "poetry", "pdm", "hatch", "conda",
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


def _install_shape(step: str, error: str) -> bool:
    """Shape 1: a known installer installing only registry packages the error names, with only flags that do not
    choose a source, target or config (`_INSTALL_FLAGS`)."""
    toks = _installer_tokens(step)
    if not toks or _PKG_URL.search(step):
        return False
    if not all(t in _INSTALL_FLAGS for t in toks[2:] if t.startswith("-")):
        return False
    return justified_by_error(step, error)


def _own_command_plus_flags(toks: list[str], failed_command: str) -> bool:
    """Shape 2: the tokens of the command that failed here, unchanged and in order, then only plain flags.

    Nothing is inserted, removed or reordered, so a stranger cannot choose the program, the subcommand or any
    argument, and cannot turn one of the user's own words into a flag's value or the other way round."""
    from .signature import _strip_prefix, program as _program
    own = _strip_prefix(_split(failed_command))
    prog = _program(failed_command) if own else ""
    if not own or not prog or own[0] != prog or toks[:len(own)] != own:
        return False
    base = base_name(prog)
    if base in _RUNNERS or base in _POWERFUL or base.split(".", 1)[0] in _POWERFUL:
        return False                     # `fsck.vfat`, `wipefs.x`: the family name counts
    added = toks[len(own):]
    if not all(_PLAIN_FLAG.fullmatch(t) and not _flag_refused(t, base) for t in added):
        return False
    if "--" in own or "-" in own:
        return False                     # after a bare `--` an added "flag" would be an argument
    words = [t.lower() for t in own[1:] if not t.startswith("-")]
    sub = words[0] if words else ""
    if any(w in _FETCH_RUN for w in words):
        return False                     # `npm exec x --yes`, `go -C dir run . -v`: not from a stranger
    if base in _PKG_TOOLS and sub not in (_ALWAYS_BUILTIN if base in _SCRIPT_BY_ANY_NAME else _MAINTENANCE):
        return False                     # `yarn build`, `cargo build`, `mvn exec:java`: they run code
    return True


def network_step_allowed(step: str, error: str, failed_command: str = "") -> bool:
    """True only for the two shapes a fix from another install may take, however many installs report it:

    1. a known installer installing registry packages the error names (`pip install httpx` for "No module named
       httpx"), with no alias, git host, path, file, URL, index, registry, source or target;
    2. the command that failed on this machine, token for token, with plain flags added at the end
       (`npm install --legacy-peer-deps` after `npm install` failed; `datactl sync --rebuild-index` after
       `datactl sync` failed). Never for interpreters, shells, runners, archivers, editors, database clients,
       cloud and deploy tools or programs whose flags alone can do harm; never for fetch-and-run subcommands
       (`exec`, `run`, `dlx`, `create` ...); for package managers and build tools only for dependency-maintenance
       subcommands; never a flag that weakens a check, destroys work or skips a confirmation, and `--force`/`-f`
       only for package-manager dependency maintenance (`npm install --force`).

    For pip, an import name that is not its package (`yaml`, `sklearn`, `cv2`) is refused: only the real package
    (`pyyaml`, `scikit-learn`, `opencv-python`) passes. Installs still cannot be told apart from dependency
    confusion (a private name registered publicly), so the engine labels every network install.

    Program names are compared by what they stand for (`npm.cmd`, `Git.exe`, `python3.12`). Text with control,
    invisible or non-plain space characters is refused. `failed_command` is the local failure as peers would write
    it (see `engine.network_shape`); without it (no such failure recorded here) only shape 1 can pass."""
    if not step or has_hidden_chars(step) or _SHELL_SYNTAX.search(step):
        return False
    toks = _split(step)
    if not toks or "/" in toks[0] or "\\" in toks[0] or re.fullmatch(r"[A-Za-z_]\w*=.*", toks[0]):
        return False
    if install_targets(step):
        return _install_shape(step, error)   # an install with no package (`npm install --x`) is the shape-2 case
    if _EXEC_ARG.search(step):
        return False
    if base_name(toks[0]) == "git" and \
            re.search(r"(?:^|\s)(?:-c|config|submodule\s+foreach|--config-env)(?:\s|=|$)", step):
        return False                     # `git -c core.x=...` / `git config` can make git run anything
    return bool(failed_command) and _own_command_plus_flags(toks, failed_command)
