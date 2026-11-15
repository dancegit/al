"""A minimal SMTP server for tests: standard library only, loopback only. Not a test module.

`Sink()` listens on 127.0.0.1 at a free port and records every delivered message in `messages`.
`mode`: `plain`, `starttls` (advertises and performs STARTTLS) or `smtps` (TLS before the
greeting). `behaviour`: `ok`, `reject-rcpt` (550 to every RCPT), `reject-auth` (535 to every
AUTH) or `hang` (accepts the connection and says nothing for 30 seconds).
`certificate(directory)` makes a throwaway self-signed certificate with the `openssl` command;
the key exists only in that directory (pytest's temporary directory), nothing is committed.
"""
from __future__ import annotations

import base64
import shutil
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path


class _Handler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")
        self.wfile.flush()

    def _secure(self) -> None:
        self.connection = self.server.context.wrap_socket(self.connection, server_side=True)
        self.rfile = self.connection.makefile("rb", -1)
        self.wfile = self.connection.makefile("wb", 0)
        self.session["tls"] = True

    def handle(self) -> None:
        server = self.server
        self.session = {"tls": False, "auth": None, "helo": "", "commands": []}
        server.sessions.append(self.session)
        try:
            if server.behaviour == "hang":
                server.closing.wait(30)
                return
            if server.mode == "smtps":
                self._secure()
            self._say("220 sink ESMTP")
            self._talk()
        except (OSError, ValueError):
            return  # the client went away, or a TLS handshake failed: the test asserts on what was recorded

    def _talk(self) -> None:
        server, session = self.server, self.session
        sender, recipients = "", []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            session["commands"].append(verb)
            if verb in ("EHLO", "HELO"):
                session["helo"] = line.partition(" ")[2].strip()
                extensions = ["sink", "AUTH PLAIN LOGIN"]   # a client skips the first line: the greeting
                if server.mode == "starttls" and not session["tls"]:
                    extensions.append("STARTTLS")
                for extension in extensions:
                    self._say(f"250-{extension}")
                self._say("250 OK")
            elif verb == "STARTTLS" and server.mode == "starttls" and not session["tls"]:
                self._say("220 go ahead")
                self._secure()
            elif verb == "AUTH":
                words = line.split()
                if server.behaviour == "reject-auth":
                    self._say("535 authentication failed")
                elif len(words) < 2 or words[1].upper() != "PLAIN":
                    self._say("504 unknown mechanism")
                else:
                    blob = words[2] if len(words) > 2 else ""
                    if not blob:
                        self._say("334 ")
                        blob = self.rfile.readline().decode("ascii", "replace").strip()
                    parts = base64.b64decode(blob).decode("utf-8", "replace").split("\0")
                    session["auth"] = (parts[-2], parts[-1]) if len(parts) >= 2 else ("", "")
                    self._say("235 authenticated")
            elif line.upper().startswith("MAIL FROM:"):
                sender = line[10:].strip().split(" ")[0].strip("<>")
                recipients = []
                self._say("250 OK")
            elif line.upper().startswith("RCPT TO:"):
                if server.behaviour == "reject-rcpt":
                    self._say("550 no such user")
                else:
                    recipients.append(line[8:].strip().split(" ")[0].strip("<>"))
                    self._say("250 OK")
            elif verb == "DATA":
                self._say("354 end with <CRLF>.<CRLF>")
                lines = []
                while True:
                    data_line = self.rfile.readline()
                    if not data_line or data_line in (b".\r\n", b".\n"):
                        break
                    lines.append(data_line[1:] if data_line.startswith(b"..") else data_line)
                server.messages.append({"tls": session["tls"], "auth": session["auth"], "helo": session["helo"],
                                        "from": sender, "to": list(recipients), "data": b"".join(lines)})
                self._say("250 accepted")
            elif verb == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("250 OK")


class Sink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mode: str = "plain", context: ssl.SSLContext | None = None, behaviour: str = "ok") -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mode, self.context, self.behaviour = mode, context, behaviour
        self.port = self.server_address[1]
        self.messages: list[dict] = []
        self.sessions: list[dict] = []
        self.closing = threading.Event()
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self) -> None:
        self.closing.set()
        self.shutdown()
        self.server_close()


def certificate(directory: Path) -> ssl.SSLContext | None:
    """A server context with a throwaway self-signed certificate for `localhost`; None without `openssl`."""
    if shutil.which("openssl") is None:
        return None
    key, cert = Path(directory) / "key.pem", Path(directory) / "cert.pem"
    try:
        made = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                               "-out", str(cert), "-days", "2", "-subj", "/CN=localhost"],
                              capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if made.returncode != 0:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    return context
