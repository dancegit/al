"""Nothing private reaches a frame: the values that would identify the machine or the person who
records (home folder, user name as a path segment, host name, git identity, addresses), checked in
every terminal transcript and every dashboard page text before anything is rendered.
Standard library only."""
from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

ALLOWED_MAIL = {"example.org", "example.com", "example.invalid", "t"}
ALLOWED_IPV4 = {"127.0.0.1", "0.0.0.0"}

IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]*\d)")
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)")


class PrivateText(Exception):
    def __init__(self, reason: str, excerpt: str) -> None:
        self.reason, self.excerpt = reason, excerpt
        super().__init__(f"{reason}: …{excerpt}…")


def _git_identity() -> list[str]:
    values = []
    for key in ("user.name", "user.email"):
        try:
            cp = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if cp.returncode == 0 and cp.stdout.strip():
            values.append(cp.stdout.strip())
    return values


def forbidden_values(home: Path | str | None = None) -> list[str]:
    """What must never show: the real home folder (pass it in when HOME has been changed since), the
    user name as a path segment `/<user>/`, the host name and its FQDN, the global git identity, and
    every non-loopback address of this host."""
    values = [str(home or Path.home())]
    for name in ("USER", "LOGNAME"):
        user = os.environ.get(name, "")
        if user:
            values.append(f"/{user}/")
    host = socket.gethostname()
    if host and host != "localhost":
        values.append(host)
        try:
            fqdn = socket.getfqdn(host)
        except OSError:
            fqdn = ""
        if fqdn and fqdn != host and fqdn != "localhost":
            values.append(fqdn)
    values += _git_identity()
    try:
        for info in socket.getaddrinfo(host, None):
            address = info[4][0]
            if not address.startswith("127.") and address not in ("::1", "0.0.0.0"):
                values.append(address)
    except OSError:
        pass
    out: list[str] = []
    for value in values:
        if value and value not in ("/", "~") and value not in out:
            out.append(value)
    return out


def _excerpt(text: str, at: int, length: int) -> str:
    return text[max(0, at - 20): at + length + 20].replace("\n", "⏎")


def check_private(text: str, forbidden: list[str] | tuple[str, ...] = (), allowed: tuple[str, ...] = ()) -> str:
    """The text, unchanged, when it shows nothing private; else PrivateText naming what and where.
    `allowed`: paths known not to be private (the recording's own neutral HOME, a temporary folder),
    passed over by the home-folder rule only; the forbidden values are looked for everywhere."""
    original = text
    for value in forbidden:
        at = text.find(value)
        if at >= 0:
            raise PrivateText("a private value of this machine", _excerpt(text, at, len(value)))
    for path in allowed:
        if path and path not in ("/", "~"):
            text = text.replace(path, "~")
    for marker in ("/home/", "/Users/"):
        at = text.find(marker)
        if at >= 0:
            raise PrivateText(f"a home folder ({marker})", _excerpt(text, at, len(marker)))
    for match in IPV4.finditer(text):
        address = match.group(1)
        if address in ALLOWED_IPV4 or not all(0 <= int(part) <= 255 for part in address.split(".")):
            continue
        raise PrivateText(f"an IPv4 address ({address})", _excerpt(text, match.start(), len(address)))
    for match in MAIL.finditer(text):
        domain = match.group(1).lower()
        if domain in ALLOWED_MAIL or "." not in domain or not re.search(r"\.[a-z]{2,}$", domain):
            continue                         # an address has a dotted domain with a top level (`pkg@1.2` is none)
        if any(domain.endswith("." + allowed) for allowed in ALLOWED_MAIL if "." in allowed):
            continue
        raise PrivateText(f"an e-mail address ({match.group(0)})", _excerpt(text, match.start(), len(match.group(0))))
    return text
