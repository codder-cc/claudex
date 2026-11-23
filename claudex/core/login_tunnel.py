"""OAuth login tunnel over a peer link.

Modern Claude Code login (`claude auth login`) is a paste-code flow: it prints a
consent URL and waits for the user to paste back an authorization code (the old
localhost-callback flow is gone). This tunnel relays that across two machines:

  * machine A runs `claude auth login` locally (the credentials land on A) and
    scrapes the consent URL from its output;
  * A sends the URL to machine B over the peer link;
  * B opens the URL in its browser, the human approves, the page shows a code,
    the human pastes it into B, and B sends the code back to A;
  * A feeds the code to its waiting `claude` process — A is now logged in, with
    the browser consent done entirely on B.

Only the URL and the code cross the link, over the same end-to-end encrypted
channel as everything else; the relay server never sees either in the clear.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import Callable, Optional

from claudex.constants import CLAUDE_BIN
from claudex.core.device import DeviceIdentity
from claudex.core.peer import PeerStore, SecureChannel, handshake

_URL_MARK = "oauth/authorize?"


# ── channel plumbing (A connects, B waits) ────────────────────────────────────
async def client_channel_lan(host: str, port: int, device: DeviceIdentity,
                             store: PeerStore, expect_device_id=None,
                             expect_pubkey=None) -> SecureChannel:
    reader, writer = await asyncio.open_connection(host, port)
    return await handshake(reader, writer, device, store, initiator=True,
                           expect_device_id=expect_device_id,
                           expect_pubkey=expect_pubkey, addr=f"{host}:{port}")


async def client_channel_relay(endpoint: str, room: str, token: str,
                               device: DeviceIdentity, store: PeerStore) -> SecureChannel:
    from claudex.fleet.peer_stream import WSStream, _relay_ws
    ws = await _relay_ws(endpoint, room, token)
    s = WSStream(ws)
    return await handshake(s, s, device, store, initiator=True, addr=f"relay:{room}")


async def serve_helper_lan(device: DeviceIdentity, store: PeerStore, run_helper_fn,
                           host: str = "0.0.0.0", port: int = 0, on_ready=None) -> None:
    done = asyncio.Event()

    async def on_conn(reader, writer):
        try:
            ch = await handshake(reader, writer, device, store, initiator=False, addr="")
            await run_helper_fn(ch)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
            done.set()

    server = await asyncio.start_server(on_conn, host, port)
    if on_ready:
        on_ready(server.sockets[0].getsockname()[1])
    try:
        await done.wait()
    finally:
        server.close()


async def serve_helper_relay(endpoint: str, room: str, token: str,
                             device: DeviceIdentity, store: PeerStore,
                             run_helper_fn) -> None:
    from claudex.fleet.peer_stream import WSStream, _relay_ws
    ws = await _relay_ws(endpoint, room, token)
    s = WSStream(ws)
    try:
        ch = await handshake(s, s, device, store, initiator=False, addr=f"relay:{room}")
        await run_helper_fn(ch)
    finally:
        await s.wait_closed()


def extract_consent_url(text: str) -> Optional[str]:
    """Pull the consent URL out of `claude auth login` output.

    Robust to ANSI/OSC-8 hyperlink escapes, which print the URL twice back to
    back; we take the first copy and cut before any duplicate.
    """
    i = text.find(_URL_MARK)
    if i < 0:
        return None
    start = text.rfind("https://", 0, i)
    if start < 0:
        return None
    end = len(text)
    for ch in ("\x1b", "\x07", "\n", "\r", " ", "\t", '"', "'"):
        j = text.find(ch, start)
        if j != -1:
            end = min(end, j)
    url = text[start:end]
    dup = url.find("https://", 8)   # the OSC-8 hyperlink text is the URL again
    if dup != -1:
        url = url[:dup]
    return url


# ── machine A: drive the local claude login, relay URL out / code in ──────────
def login_argv(email: Optional[str] = None, console: bool = False) -> list:
    argv = [CLAUDE_BIN, "auth", "login", "--console" if console else "--claudeai"]
    if email:
        argv += ["--email", email]
    return argv


async def run_initiator(ch: SecureChannel, *, email: Optional[str] = None,
                        console: bool = False, config_dir: Optional[str] = None,
                        on_status: Optional[Callable[[str], None]] = None,
                        url_timeout: float = 30.0,
                        code_timeout: float = 600.0) -> bool:
    """A side: run `claude auth login`, send its URL to the peer, feed back the
    code the peer returns. Returns True if claude exited cleanly."""
    def say(m: str) -> None:
        if on_status:
            on_status(m)

    env = None
    if config_dir:
        import os
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = config_dir

    proc = await asyncio.create_subprocess_exec(
        *login_argv(email=email, console=console),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env)

    # Scrape the consent URL from the login output.
    buf = ""
    url = None
    deadline = asyncio.get_event_loop().time() + url_timeout
    while url is None:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            proc.kill()
            say("timed out waiting for the consent URL from claude")
            return False
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
        except asyncio.TimeoutError:
            continue
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        url = extract_consent_url(buf)

    if not url:
        proc.kill()
        say("could not find the consent URL in claude's output")
        return False

    say("sending the consent URL to the peer…")
    await ch.send({"op": "login_url", "url": url, "email": email})

    reply = await asyncio.wait_for(ch.recv(), timeout=code_timeout)
    if not reply or reply.get("op") != "login_code" or not reply.get("code"):
        proc.kill()
        say("peer did not return a code")
        return False
    code = str(reply["code"]).strip()

    say("got the code from the peer — finishing login locally…")
    proc.stdin.write((code + "\n").encode("utf-8"))
    await proc.stdin.drain()
    try:
        proc.stdin.write_eof()
    except Exception:  # noqa: BLE001
        pass
    rc = await asyncio.wait_for(proc.wait(), timeout=60)
    await ch.send({"op": "login_done", "ok": rc == 0})
    return rc == 0


# ── machine B: show the URL, collect the pasted code, relay it back ───────────
async def run_helper(ch: SecureChannel, *,
                     open_url: Optional[Callable[[str], None]] = None,
                     get_code: Optional[Callable[[str], str]] = None,
                     on_status: Optional[Callable[[str], None]] = None) -> bool:
    """B side: receive the URL, open it + collect the pasted code, send it back."""
    def say(m: str) -> None:
        if on_status:
            on_status(m)

    msg = await ch.recv()
    if not msg or msg.get("op") != "login_url" or not msg.get("url"):
        say("peer did not send a login URL")
        return False
    url = msg["url"]

    if open_url is not None:
        try:
            open_url(url)
        except Exception:  # noqa: BLE001
            pass
    if get_code is None:
        def get_code(_u: str) -> str:
            return input("paste the code from the page: ").strip()
    code = await asyncio.get_event_loop().run_in_executor(None, lambda: get_code(url))
    await ch.send({"op": "login_code", "code": str(code).strip()})

    done = await ch.recv()
    ok = bool(done and done.get("op") == "login_done" and done.get("ok"))
    say("login succeeded on the peer" if ok else "login did not complete on the peer")
    return ok
