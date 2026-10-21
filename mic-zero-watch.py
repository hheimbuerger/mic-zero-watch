#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets>=13"]
# ///
"""Detect a muted AnkerWork M650 transmitter and mirror it into Google Meet.

A muted (or switched-off) AnkerWork M650 transmitter makes its USB receiver
send exact digital zeros, while an unmuted mic always carries some room noise,
even in a silent room. The mute itself is never reported over USB. So a run
of 50 ms blocks where every sample is 0 means "muted".

Background mode (default) listens on ws://127.0.0.1:2395, where the Meet
browser extension (streamdeck-googlemeet, branch feat/mic-watcher-control)
connects while a Meet tab is open. While it is connected, the AnkerWork is
watched, and:

- transmitter muted   -> mute Meet, if Meet's mic was on; remember the mute
                         as ours
- transmitter unmuted -> unmute Meet, but only if the mute is still ours

Any Meet mic change that didn't come from us (Stream Deck, clicking in Meet,
Ctrl+D) drops the "ours" mark, so we never undo somebody else's mute.

--live shows a one-line live view instead and always watches the mic. It
also drives Meet, unless the background service already holds the port.

The mic is read through PipeWire as an ordinary shared stream, so voxtype,
Meet etc. keep working.
"""

import argparse
import asyncio
import json
import math
import shutil
import signal
import sys
import time

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

RATE = 16000
BLOCK_SAMPLES = RATE // 20          # 50 ms
ZERO_BLOCKS_FOR_MUTE = 3            # 150 ms of exact zeros
LIVE_BLOCKS_FOR_UNMUTE = 2          # 100 ms of signal
MIC_GLOB_PREFIX = "alsa_input.usb-Actions_AnkerWork_M650_RX_"
MEET_PORT = 2395
DEVICE_POLL_S = 2.0
READ_TIMEOUT_S = 1.0
COMMAND_CONFIRM_S = 3.0
FLOOR_DB = -90.0


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


async def find_mic(explicit):
    """Return the node.name to watch, or None if no AnkerWork is attached."""
    proc = await asyncio.create_subprocess_exec(
        "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        objects = json.loads(out)
    except json.JSONDecodeError:
        return None
    for obj in objects:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Audio/Source":
            continue
        name = props.get("node.name", "")
        if (explicit and name == explicit) or (not explicit and name.startswith(MIC_GLOB_PREFIX)):
            return name
    return None


class Meet:
    """The Meet extension's side: connections, Meet's mic state, our mute mark.

    Acts only when the transmitter changes, never on its steady state: between
    two presses of the transmitter's mute button, Meet is the user's to control.
    """

    def __init__(self):
        self.connections = set()
        self.muted = None           # Meet's mic state; None until reported
        self.ours = False           # Meet is muted because we muted it
        self.pending = None         # (expected_state, deadline) after our command
        self.tx_muted = False       # transmitter state, as last reported
        self.changed = asyncio.Event()

    @property
    def connected(self):
        return bool(self.connections)

    async def handler(self, conn):
        self.connections.add(conn)
        log("Meet extension connected")
        self.changed.set()
        try:
            await conn.send(json.dumps({"event": "getMicState"}))
            async for raw in conn:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") == "micMutedState":
                    await self._on_state(bool(msg.get("muted")))
        except ConnectionClosed:
            pass
        finally:
            self.connections.discard(conn)
            if not self.connections:
                log("Meet extension disconnected")
                self.muted, self.ours, self.pending = None, False, None
            self.changed.set()

    async def _on_state(self, muted):
        first = self.muted is None
        if self.pending and time.monotonic() > self.pending[1]:
            log("Meet didn't confirm our command, giving up on it")
            self.pending = None
        if self.pending and self.pending[0] == muted:
            # The change we asked for.
            self.pending = None
            log(f"Meet {'muted' if muted else 'unmuted'} (by us)")
        elif not first and muted != self.muted:
            # Somebody else changed Meet's mic: Stream Deck, a click, Ctrl+D.
            if self.ours:
                log(f"Meet {'muted' if muted else 'unmuted'} from elsewhere, "
                    "handing control back")
            self.ours, self.pending = False, None
        self.muted = muted
        if first and self.tx_muted and not muted:
            # Meet (re)connected while the transmitter was already muted, so
            # we couldn't act on the press itself: catch up once.
            await self._mute()

    async def _send(self, event, expected):
        self.pending = (expected, time.monotonic() + COMMAND_CONFIRM_S)
        payload = json.dumps({"event": event})
        for conn in list(self.connections):
            try:
                await conn.send(payload)
            except ConnectionClosed:
                pass

    async def _mute(self):
        self.ours = True
        await self._send("muteMic", True)

    async def set_transmitter(self, tx_muted):
        """Called once per transmitter change, not per audio block."""
        self.tx_muted = tx_muted
        if not self.connected:
            return
        if tx_muted:
            if self.muted is False:
                await self._mute()
        elif self.ours:
            self.ours = False
            # Also covers a mute that is still on its way to Meet.
            if self.muted or (self.pending and self.pending[0]):
                await self._send("unmuteMic", False)


class Live:
    """One self-overwriting status line."""

    def __init__(self, meet):
        self.meet = meet

    def show(self, mic, peak, state):
        if mic is None:
            text = "\033[90mno AnkerWork receiver connected\033[0m"
        else:
            db = 20 * math.log10(peak / 32768) if peak else float("-inf")
            label = {"muted": "\033[1;31mMUTED / NO SIGNAL\033[0m",
                     "zero": "\033[33mzero block       \033[0m",
                     "live": "\033[32mlive             \033[0m"}[state]
            num = f"{db:6.1f} dBFS" if peak else "  -inf dBFS"
            cols = shutil.get_terminal_size((100, 20)).columns
            meet = self._meet_text()
            width = max(10, cols - 52)
            filled = round(width * (db - FLOOR_DB) / -FLOOR_DB) if db > FLOOR_DB else 0
            text = f"{label}  {num}  [{'#' * filled}{'.' * (width - filled)}]  {meet}"
        sys.stdout.write("\r\033[K" + text)
        sys.stdout.flush()

    def _meet_text(self):
        m = self.meet
        if m is None:
            return "Meet: n/a"
        if not m.connected:
            return "Meet: -"
        state = {None: "?", True: "muted", False: "on"}[m.muted]
        return f"Meet: {state}{'*' if m.ours else ''}"


async def watch_mic(mic, meet, live):
    """Read one mic until it disappears or watching is no longer wanted."""
    cmd = ["pw-record", "--raw", "--format", "s16", "--rate", str(RATE),
           "--channels", "1", "--latency", "50ms", "--target", mic,
           "-P", "{ node.name = mic-zero-watch media.name = mic-zero-watch "
                 "node.dont-fallback = true node.dont-reconnect = true }",
           "-"]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    zero_run = live_run = 0
    tx_muted = False
    if meet:
        meet.tx_muted = False   # unknown again until the first blocks arrive
    nbytes = BLOCK_SAMPLES * 2
    try:
        while live or meet.connected:
            try:
                buf = await asyncio.wait_for(proc.stdout.readexactly(nbytes), READ_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return  # mic gone or stream stalled; the caller re-resolves it
            samples = memoryview(buf).cast("h")
            peak = max(max(samples), -min(samples))
            if peak == 0:
                zero_run, live_run = zero_run + 1, 0
            else:
                zero_run, live_run = 0, live_run + 1

            changed = False
            if not tx_muted and zero_run >= ZERO_BLOCKS_FOR_MUTE:
                tx_muted = changed = True
                if not live:
                    log("transmitter muted (no signal)")
            elif tx_muted and live_run >= LIVE_BLOCKS_FOR_UNMUTE:
                tx_muted, changed = False, True
                if not live:
                    log("transmitter unmuted")
            if meet and changed:
                await meet.set_transmitter(tx_muted)
            if live:
                live.show(mic, peak, "muted" if tx_muted else "zero" if peak == 0 else "live")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()


async def run(args):
    # systemd stops us with SIGTERM: unwind normally so pw-record is reaped.
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, asyncio.current_task().cancel)
    meet = Meet()
    server = None
    try:
        server = await serve(meet.handler, "127.0.0.1", MEET_PORT)
    except OSError:
        if not args.live:
            log(f"port {MEET_PORT} is already in use; is another instance running?")
            return 1
        meet = None  # live view only; the background service drives Meet
    live = Live(meet) if args.live else None
    if not args.live:
        log(f"listening for the Meet extension on 127.0.0.1:{MEET_PORT}")

    try:
        while True:
            if not args.live and not meet.connected:
                # Nothing to do without Meet; don't hold the mic open.
                meet.changed.clear()
                await meet.changed.wait()
                continue
            mic = await find_mic(args.target)
            if mic is None:
                if live:
                    live.show(None, 0, "live")
                await asyncio.sleep(DEVICE_POLL_S)
                continue
            if not args.live:
                log(f"watching {mic}")
            await watch_mic(mic, meet, live)
    finally:
        if server:
            server.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true",
                        help="show a one-line live view instead of logging")
    parser.add_argument("--target", metavar="NODE_NAME",
                        help="watch this PipeWire node instead of the AnkerWork receiver")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        if args.live:
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()
