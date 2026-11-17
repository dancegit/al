"""Telling a person when a run needs them or has ended: the layer's `(notify …)` options.

Events. A run reports exactly one event (`event_of`):
- `needs-human`: the run failed and was handed over (state.ship["action"] == "handoff" without a
  handoff_error), or a spending or usage limit stopped it, or the run finished `handed-over` (a
  rejected review or a gate that fell back, see state.ship["handed_over"]);
- `failed`: the run failed otherwise;
- `shipped`: the run passed and state.ship["shipped"] is true;
- `passed`: the run passed otherwise (also when a pull request or PR.md waits for a review).
A run the stand-in decider is resuming (state.decision["status"] == "resuming", see decide.py)
reports nothing: the resumed run reports its own outcome.

Values. A notifier names environment variables; their values come from the process environment,
`<layer>/local.env` and `$XDG_CONFIG_HOME/al/<layer name>.env` (see localenv). File values stay in
a dictionary only this module reads: they are never put into os.environ, state, events or logs.
Only the child process of a `command` notifier gets them in its environment.

SMTP URL (`parse_smtp_url`): `smtp://host[:port]` (plain, port 25), `smtp+starttls://[user:pass@]host[:port]`
(587), `smtps://[user:pass@]host[:port]` (465); user and password are percent-decoded. Plain
`smtp://` to a host that is not loopback is refused unless the URL ends in `?insecure=plaintext`.
Certificates are verified except for a loopback host. STARTTLS is never downgraded.

The message (`compose`) is short and safe: the subject `[al] <layer>: <event in words> — <title>`
and a few lines (workflow, run id, one or two sentences, where to look). No prompt text beyond the
title, no log tail, no environment, no machine path, no host name.

The decision block. When the stand-in decider looked at the run (state.decision has an action),
the message adds "Decision (stand-in for a person)": the mode, the action, the step it resumes
from and the decider's one-line summary (as a reason: first line, no machine path, at most
REASON_MAX characters). In `suggest` mode it also says how to answer (`go`, `alter: …`, `explain`,
`reject`, or `al answer LAYER RUN go`) and the subject ends in `[decide RUN-CODE]`: a reply by
e-mail or webhook must carry CODE. `send_decision` sends the decider's explanation (the answer
`explain`) as one more message through the notifiers that report `needs-human`.

Runtime (`report`): called by the runner right after a handover (sends only `needs-human`) and at
the end of a run. state.notified is written before each attempt, so there is at most one attempt
per run, event and notifier, also over --resume. Each attempt is bounded by the notifier's
timeout. `report` never raises and never changes the run's result. A fan-out candidate sends
nothing (its parent reports); a --dry-run sends nothing unless --notify is given.

Failure reasons are a fixed vocabulary that never contains a value (a recipient, a host, a
password); an exception's text is never used, only its class name and SMTP code:
- `variable <NAME> is not set`
- `<NAME> is not a usable SMTP URL: <why>`, with <why> one of `unknown scheme (use smtp://,
  smtp+starttls:// or smtps://)`, `no host`, `bad port`, `unknown option (only ?insecure=plaintext
  exists, for smtp://)`; and PLAINTEXT_REFUSED
- `<NAME> holds no valid address`
- `timed out after <N> s`
- `the server does not offer STARTTLS`
- `login refused (SMTP <code>)`, `sender refused (SMTP <code>)`, `recipient refused (SMTP <code>)`,
  `message refused (SMTP <code>)`
- `TLS failed (<exception class>)`, `could not connect (<exception class>)`, `<exception class>`
  for anything else
- command: `exit <N>`, `could not start (<exception class>)`
"""
from __future__ import annotations

import email.message
import email.policy
import email.utils
import os
import re
import signal
import smtplib
import ssl
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import events, localenv

EVENTS = ("needs-human", "failed", "passed", "shipped")
WORDS = {"needs-human": "needs a person", "failed": "failed", "passed": "finished", "shipped": "shipped"}
DASHBOARD_ENV = "AL_DASHBOARD_URL"
DEFAULT_TIMEOUT = 10
REASON_MAX = 300
PLAINTEXT_REFUSED = ("refused: plain smtp:// to a host that is not this machine would send the message and any "
                     "password unencrypted; use smtp+starttls:// or smtps://, or end the URL with "
                     "?insecure=plaintext to accept that")
_DEFAULT_PORTS = {"smtp": 25, "smtp+starttls": 587, "smtps": 465}
_LOOPBACK_V4 = re.compile(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}")
_SUBJECT_CONTROL = re.compile(r"[\x00-\x1f]")
_BODY_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f]")


class NotifyError(Exception):
    """A notifier could not deliver. Its text is one of the fixed reasons of the module docstring,
    safe to record."""


def notifiers(layer: Any) -> list[dict[str, Any]]:
    """The layer's notifiers that send something (a layer compiled before notifications has none)."""
    declared = layer.ir.get("notifiers") or []
    return [item for item in declared if isinstance(item, dict) and item.get("kind") in ("email", "command")]


def event_of(state: Any) -> str:
    """The one event a run reports ("" for a waiting run: it neither failed nor finished)."""
    if state.status == "waiting":
        return ""
    if (getattr(state, "decision", None) or {}).get("status") == "resuming":
        return ""  # the decider resumes the run: the resumed run reports its own outcome
    ship = state.ship or {}
    if state.status == "handed-over":
        return "needs-human"
    if state.status != "passed":
        if ship.get("action") == "handoff" and not ship.get("handoff_error"):
            return "needs-human"
        if (state.budget or {}).get("stopped") or (state.usage or {}).get("stopped"):
            return "needs-human"
        return "failed"
    return "shipped" if ship.get("shipped") is True else "passed"


# ---------------------------------------------------------------- the SMTP URL

def loopback(host: str) -> bool:
    """True for this machine by name or address; a literal comparison, no DNS."""
    name = host.lower().strip("[]")
    return name in ("localhost", "localhost.localdomain", "::1") or bool(_LOOPBACK_V4.fullmatch(name))


@dataclass(frozen=True)
class SmtpUrl:
    scheme: str
    host: str
    port: int
    user: str
    password: str


def parse_smtp_url(text: str, name: str) -> SmtpUrl:
    """The parts of an SMTP URL. `name` is the variable that holds it: an error names the variable,
    never the URL's text."""
    def unusable(why: str) -> NotifyError:
        return NotifyError(f"{name} is not a usable SMTP URL: {why}")

    try:
        parts = urllib.parse.urlsplit(text.strip())
        host = parts.hostname or ""
    except ValueError:
        raise unusable("no host") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise unusable("unknown scheme (use smtp://, smtp+starttls:// or smtps://)")
    if not host:
        raise unusable("no host")
    try:
        port = parts.port
    except ValueError:
        raise unusable("bad port") from None
    if port is None:
        if parts.netloc.rsplit("@", 1)[-1].endswith(":"):
            raise unusable("bad port")
        port = _DEFAULT_PORTS[scheme]
    if not 1 <= port <= 65535:
        raise unusable("bad port")
    insecure = scheme == "smtp" and parts.query == "insecure=plaintext"
    if parts.path not in ("", "/") or parts.fragment or (parts.query and not insecure):
        raise unusable("unknown option (only ?insecure=plaintext exists, for smtp://)")
    if scheme == "smtp" and not loopback(host) and not insecure:
        raise NotifyError(PLAINTEXT_REFUSED)
    return SmtpUrl(scheme, host, port, urllib.parse.unquote(parts.username or ""),
                   urllib.parse.unquote(parts.password or ""))


def tls_context(host: str) -> ssl.SSLContext:
    """Certificates are verified, except on a loopback host (where plain SMTP is accepted anyway),
    so a local mail bridge with a self-signed certificate works."""
    context = ssl.create_default_context()
    if loopback(host):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


# ---------------------------------------------------------------- the message

@dataclass(frozen=True)
class Message:
    event: str
    subject: str
    body: str


def _reason(layer: Any, state: Any, text: str) -> str:
    """A reason as it may travel: its first line, without machine paths, at most REASON_MAX characters."""
    from . import ship

    first = next((line for line in str(text).splitlines() if line.strip()), "")
    first = ship._without_machine_paths(layer, state, first).replace(str(Path.home()), "~")
    first = " ".join(first.split())
    return first if len(first) <= REASON_MAX else first[:REASON_MAX - 1].rstrip() + "…"


def _clean(subject: str, lines: list[str]) -> tuple[str, str]:
    return _SUBJECT_CONTROL.sub("", subject), _BODY_CONTROL.sub("", "\n".join(lines) + "\n")


def _failed_step(state: Any) -> tuple[str, str]:
    """(name, detail) of the first failed step; ("run", state.error or a fixed text) without one."""
    for record in state.steps or []:
        if record.get("status") == "failed":
            name = str(record.get("name") or "run")
            return name, str(record.get("detail") or "").removeprefix(f"{name}: ")
    return "run", str(getattr(state, "error", "") or "")


def _sentences(layer: Any, state: Any, event: str, url: str) -> str:
    ship_record = state.ship or {}
    if event == "needs-human" and state.status == "handed-over":
        why = _reason(layer, state, ", ".join(str(r) for r in ship_record.get("handed_over") or []))
        return f"The run finished and was handed to a person. Why: {why}"
    if event in ("needs-human", "failed"):
        step, detail = _failed_step(state)
        handed = ship_record.get("action") == "handoff" and not ship_record.get("handoff_error")
        if event == "needs-human":
            stop = (state.budget or {}).get("stopped") or (state.usage or {}).get("stopped") or {}
            reasons = ship_record.get("reasons") or []
            handover = str(reasons[0]) if handed and reasons else ""
            match = re.match(r"run-stopped:([^:]*): ", handover)
            if handed:
                step = str(ship_record.get("stopped_at") or (match.group(1) if match else "") or step)
            elif stop.get("step"):
                step = str(stop["step"])
            detail = (handover[match.end():] if match else handover) or str(stop.get("reason") or "") or detail
        reason = _reason(layer, state, detail) or "no step recorded a reason"
        if event == "failed":
            return f"The run failed at step `{step}`. Why: {reason}"
        return (f"The run stopped at step `{step}` and "
                + ("was handed to a person" if handed else "needs a person") + f". Why: {reason}")
    if event == "shipped":
        target = _reason(layer, state, str(ship_record.get("target") or ""))
        return "The run finished and its change was shipped" + (f" ({target})." if target else ".")
    if url:
        return "The run finished. A pull request waits for a person."
    if ship_record.get("action") == "pr":
        return "The run finished. The work is on its branch and waits for a person."
    return "The run finished."


def compose(layer: Any, state: Any, event: str, values: dict[str, str]) -> Message:
    """The message of a run's event: a subject and a few lines, nothing private (module docstring)."""
    from . import ship

    ship_record = state.ship or {}
    pr = ship_record.get("pr")
    url = str(ship_record.get("url") or (pr.get("url") if isinstance(pr, dict) else "") or "")
    where = []
    branch = str(ship_record.get("branch") or state.branch or "")
    if branch:
        where.append(f"- branch: {branch}")
    if url:
        where.append(f"- pull request: {url}")
    description = Path(layer.layer_dir) / "agents" / state.adw_id / "PR.md"
    if description.is_file():
        where.append(f"- description: {layer.display_path(description)}")
    dashboard = localenv.lookup(DASHBOARD_ENV, values).strip()
    if dashboard:
        where.append(f"- dashboard: {dashboard}")
    lines = [f"Workflow: {state.adw}", f"Run: {state.adw_id}", "", _sentences(layer, state, event, url)]
    lines += _decision_lines(layer, state) if event == "needs-human" else []
    if where:
        lines += ["", "Where to look:", *where]
    subject = f"[al] {layer.name}: {WORDS[event]} — {ship._title(state, layer)}"
    if event == "needs-human" and _answerable(state):
        subject += f" {decision_token(state)}"
    subject, body = _clean(subject, lines)
    return Message(event, subject, body)


def _shown_decision(state: Any) -> dict[str, Any]:
    """The decision a message shows: one with an action, not skipped, not the one that resumed."""
    decision = getattr(state, "decision", None) or {}
    if not decision.get("action") or decision.get("skipped") or decision.get("status") in ("resuming", "resumed", "failed"):
        return {}
    return decision


def _answerable(state: Any) -> bool:
    decision = _shown_decision(state)
    return decision.get("mode") == "suggest" and decision.get("status") in ("suggested", "explained")


def decision_token(state: Any) -> str:
    """`[decide RUN-CODE]`: an answer by e-mail or webhook repeats it."""
    return f"[decide {state.adw_id}-{state.decision.get('code', '')}]"


def _decision_lines(layer: Any, state: Any) -> list[str]:
    decision = _shown_decision(state)
    if not decision:
        return []
    lines = ["", "Decision (stand-in for a person):", f"- mode: {decision.get('mode')}",
             f"- action: {decision.get('action')}"]
    if decision.get("action") in ("resume", "resume-with"):
        lines.append(f"- from step: {decision.get('from_step') or '?'}")
    lines.append(f"- summary: {_reason(layer, state, str(decision.get('summary') or ''))}")
    return lines + (_answer_lines(state) if _answerable(state) else [])


def _answer_lines(state: Any) -> list[str]:
    return ["", "To answer, reply with one line (keep the subject), or run the command:",
            "- go: apply the suggestion", "- alter: <your changes>: apply it with your changes",
            "- explain: send the decider's full explanation", "- reject: leave the run with a person",
            f"- on the layer's machine: al answer LAYER {state.adw_id} go"]


def compose_test(layer: Any, event: str, values: dict[str, str]) -> Message:
    """The marked message of `al notify-test`: no run is behind it."""
    lines = ["This is a test message sent by `al notify-test`. No run happened and nothing needs you.", "",
             "Workflow: (none)", "Run: (none)", "",
             f"A real message for the event `{event}` has this form: workflow, run id, what happened in one or two "
             "sentences, where to look."]
    dashboard = localenv.lookup(DASHBOARD_ENV, values).strip()
    if dashboard:
        lines += ["", "Where to look:", f"- dashboard: {dashboard}"]
    subject, body = _clean(f"[al] {layer.name}: TEST, {WORDS[event]} — a test message from al notify-test", lines)
    return Message(event, subject, body)


# ---------------------------------------------------------------- delivery

def _timeout(notifier: dict[str, Any]) -> int:
    return int(notifier.get("timeout") or DEFAULT_TIMEOUT)


def _bounded(function: Callable[[], Any], seconds: float) -> tuple[bool, Any]:
    """Run `function` in a daemon thread for at most `seconds`: (False, None) when it is still
    running (the thread is abandoned; its socket has the same timeout), else (True, its result or
    the exception it raised)."""
    box: list[Any] = [None]

    def target() -> None:
        try:
            box[0] = function()
        except BaseException as exc:  # noqa: BLE001 — handed to the caller, which maps it to a fixed reason
            box[0] = exc

    thread = threading.Thread(target=target, name="notify", daemon=True)
    thread.start()
    thread.join(seconds)
    return (False, None) if thread.is_alive() else (True, box[0])


def _smtp_failure(exc: BaseException, seconds: int) -> NotifyError:
    """An exception of the SMTP conversation as a fixed reason; never str(exc)."""
    def code(value: Any) -> str:
        return str(value) if isinstance(value, int) else "?"

    if isinstance(exc, NotifyError):
        return exc
    if isinstance(exc, TimeoutError) or (isinstance(exc, smtplib.SMTPServerDisconnected)
                                         and isinstance(exc.__context__, TimeoutError)):
        # smtplib wraps a read that timed out in SMTPServerDisconnected (the TimeoutError is its context)
        return NotifyError(f"timed out after {seconds} s")
    if isinstance(exc, ssl.SSLError):
        return NotifyError(f"TLS failed ({type(exc).__name__})")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return NotifyError(f"login refused (SMTP {code(exc.smtp_code)})")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return NotifyError(f"sender refused (SMTP {code(exc.smtp_code)})")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        first = next(iter((exc.recipients or {}).values()), (None, b""))
        return NotifyError(f"recipient refused (SMTP {code(first[0])})")
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
        return NotifyError(f"could not connect ({type(exc).__name__})")
    if isinstance(exc, smtplib.SMTPResponseException):
        return NotifyError(f"message refused (SMTP {code(exc.smtp_code)})")
    if isinstance(exc, smtplib.SMTPException):
        return NotifyError(type(exc).__name__)
    if isinstance(exc, OSError):
        return NotifyError(f"could not connect ({type(exc).__name__})")
    return NotifyError(type(exc).__name__)


def _send_email(notifier: dict[str, Any], message: Message, values: dict[str, str]) -> None:
    """Send `message` over SMTP. Raises NotifyError with a fixed reason."""
    seconds = _timeout(notifier)
    names = {key: str(notifier.get(key) or "") for key in ("toEnv", "fromEnv", "smtpUrlEnv")}
    found = {key: localenv.lookup(name, values).strip() if name else "" for key, name in names.items()}
    for key in ("toEnv", "fromEnv", "smtpUrlEnv"):
        if not found[key]:
            raise NotifyError(f"variable {names[key]} is not set")
    url = parse_smtp_url(found["smtpUrlEnv"], names["smtpUrlEnv"])
    recipients = [address for _name, address in email.utils.getaddresses([found["toEnv"]]) if "@" in address]
    if not recipients:
        raise NotifyError(f"{names['toEnv']} holds no valid address")
    sender = email.utils.parseaddr(found["fromEnv"])[1]
    if "@" not in sender:
        raise NotifyError(f"{names['fromEnv']} holds no valid address")
    mail = email.message.EmailMessage(policy=email.policy.default.clone(cte_type="7bit"))
    try:
        mail["From"] = found["fromEnv"]
    except (ValueError, IndexError):
        raise NotifyError(f"{names['fromEnv']} holds no valid address") from None
    try:
        mail["To"] = ", ".join(recipients)
    except (ValueError, IndexError):
        raise NotifyError(f"{names['toEnv']} holds no valid address") from None
    mail["Subject"] = message.subject
    mail["Date"] = email.utils.formatdate(localtime=False)
    try:
        mail["Message-ID"] = email.utils.make_msgid(domain=sender.rsplit("@", 1)[1])
    except (ValueError, IndexError):
        raise NotifyError(f"{names['fromEnv']} holds no valid address") from None
    mail["Auto-Submitted"] = "auto-generated"
    mail.set_content(message.body)
    try:
        if url.scheme == "smtps":
            server: smtplib.SMTP = smtplib.SMTP_SSL(url.host, url.port, timeout=seconds, context=tls_context(url.host),
                                                    local_hostname="localhost")
        else:
            server = smtplib.SMTP(url.host, url.port, timeout=seconds, local_hostname="localhost")
        with server:
            if url.scheme == "smtp+starttls":
                try:
                    server.starttls(context=tls_context(url.host))
                except smtplib.SMTPNotSupportedError:
                    raise NotifyError("the server does not offer STARTTLS") from None
            if url.user:
                server.login(url.user, url.password)
            refused = server.send_message(mail, from_addr=sender, to_addrs=recipients)
            if refused:
                first = next(iter(refused.values()), (None, b""))
                raise NotifyError(f"recipient refused (SMTP {first[0] if isinstance(first[0], int) else '?'})")
    except Exception as exc:  # noqa: BLE001 — every failure becomes one of the fixed reasons
        raise _smtp_failure(exc, seconds) from None


def _run_command(layer: Any, notifier: dict[str, Any], message: Message, values: dict[str, str]) -> None:
    """Run the notifier's command with the message on standard input. Raises NotifyError with a fixed reason."""
    seconds = _timeout(notifier)
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", str(notifier["run"])], cwd=layer.layer_dir, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            env={**values, **os.environ, "AL_NOTIFY_EVENT": message.event, "AL_NOTIFY_SUBJECT": message.subject})
    except (OSError, ValueError, KeyError) as exc:
        raise NotifyError(f"could not start ({type(exc).__name__})") from None
    try:
        process.communicate((message.subject + "\n\n" + message.body).encode(), timeout=seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group ended between the timeout and the kill
        try:
            process.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise NotifyError(f"timed out after {seconds} s") from None
    if process.returncode != 0:
        raise NotifyError(f"exit {process.returncode}")


def deliver(layer: Any, notifier: dict[str, Any], message: Message, values: dict[str, str]) -> dict[str, Any]:
    """One bounded attempt: {"accepted": bool, "reason": a fixed reason or "", "seconds": float}. Never raises."""
    started = time.monotonic()
    reason = ""
    try:
        seconds = _timeout(notifier)
        if notifier.get("kind") == "email":
            finished, outcome = _bounded(lambda: _send_email(notifier, message, values), seconds)
            if not finished:
                reason = f"timed out after {seconds} s"
            elif isinstance(outcome, NotifyError):
                reason = str(outcome)
            elif isinstance(outcome, BaseException):
                reason = type(outcome).__name__
        else:
            _run_command(layer, notifier, message, values)
    except NotifyError as exc:
        reason = str(exc)
    except Exception as exc:  # noqa: BLE001 — a notifier never fails its caller; only the class is shown
        reason = type(exc).__name__
    return {"accepted": not reason, "reason": reason, "seconds": time.monotonic() - started}


# ---------------------------------------------------------------- a run reports; al notify-test

def report(layer: Any, state: Any, force: bool = False, at: str = "end") -> bool:
    """Send the run's event through every notifier that lists it and has not reported it yet.
    True when at least one attempt was made (the caller saves the state). Never raises."""
    try:
        declared = notifiers(layer)
        if not declared or state.fanout_parent:
            return False
        event = event_of(state)
        if not event:
            return False
        if at == "handover" and event != "needs-human":
            return False
        due = [item for item in declared
               if event in (item.get("events") or []) and event not in state.notified.get(str(item.get("id")), [])]
        if not due:
            return False
        if state.dry_run and not force:
            print(f"[adw {state.adw_id}] notify: dry run, nothing sent for {event} (pass --notify to send)")
            return False
        values = localenv.load(layer)
        message = compose(layer, state, event, values)
        for notifier in due:
            nid, kind = str(notifier.get("id")), str(notifier.get("kind"))
            state.notified.setdefault(nid, []).append(event)
            state.save(layer.layer_dir)  # before the attempt: a run killed while sending never sends twice
            result = deliver(layer, notifier, message, values)
            if result["accepted"]:
                events.say(f"[adw {state.adw_id}] notify: {nid} ({kind}) {event}: sent", "notify.sent",
                           notifier=nid, kind=kind, event=event, seconds=round(result["seconds"], 3))
            else:
                events.say(f"[adw {state.adw_id}] notify: {nid} ({kind}) {event}: FAILED: {result['reason']}",
                           "notify.failed", notifier=nid, kind=kind, event=event,
                           seconds=round(result["seconds"], 3), reason=result["reason"])
        return True
    except Exception as exc:  # noqa: BLE001 — a notification never fails or changes a run
        print(f"[adw {getattr(state, 'adw_id', '?')}] warning: notify failed: {type(exc).__name__}")
        return False


def send_decision(layer: Any, state: Any, text: str) -> int:
    """The answer `explain`: one message with the decider's explanation (`text`, already without
    machine paths) through every notifier that reports `needs-human`. Each attempt is recorded in
    state.decision["sent"]. Returns how many notifiers accepted it. Never raises."""
    from . import ship

    accepted = 0
    try:
        due = [item for item in notifiers(layer) if "needs-human" in (item.get("events") or [])]
        values = localenv.load(layer)
        lines = [f"Workflow: {state.adw}", f"Run: {state.adw_id}", "",
                 "The stand-in decider explains its suggestion "
                 f"({state.decision.get('action')} from step {state.decision.get('from_step') or '?'}):", "",
                 *str(text).splitlines()]
        lines += _answer_lines(state) if _answerable(state) else []
        subject, body = _clean(f"[al] {layer.name}: the decider explains — {ship._title(state, layer)} "
                               f"{decision_token(state)}", lines)
        message = Message("needs-human", subject, body)
        sent = state.decision.setdefault("sent", [])
        for notifier in due:
            nid, kind = str(notifier.get("id")), str(notifier.get("kind"))
            result = deliver(layer, notifier, message, values)
            sent.append({"notifier": nid, "kind": kind, "what": "explanation", "accepted": result["accepted"],
                         "reason": result["reason"]})
            accepted += int(bool(result["accepted"]))
            print(f"[adw {state.adw_id}] notify: {nid} ({kind}) explanation: "
                  + ("sent" if result["accepted"] else f"FAILED: {result['reason']}"))
    except Exception as exc:  # noqa: BLE001 — an explanation never fails the answer that asked for it
        print(f"[adw {getattr(state, 'adw_id', '?')}] warning: notify failed: {type(exc).__name__}")
    return accepted


def test(layer: Any, event: str) -> list[dict[str, Any]]:
    """`al notify-test`: one marked test message through every notifier, whether or not it lists
    `event`. One row per notifier: id, kind, reports (does it list the event), accepted, reason,
    seconds. No state, no events."""
    values = localenv.load(layer)
    message = compose_test(layer, event, values)
    return [{"id": str(notifier.get("id")), "kind": str(notifier.get("kind")),
             "reports": event in (notifier.get("events") or []), **deliver(layer, notifier, message, values)}
            for notifier in notifiers(layer)]
