#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets>=13"]
# ///
"""Detect a muted wireless mic transmitter and mirror it into Google Meet.

Some wireless mics, like the AnkerWork M650, never report their transmitter's
mute button to the computer, but a muted transmitter makes the receiver send
exact digital zeros, while an unmuted mic always carries some room noise, even
in a silent room. So a run of 50 ms blocks where every sample is 0 means
"muted". Only mics verified to behave like this may be watched: many mics with
a noise gate also send zeros whenever you're quiet. Which mics are watched is
set in the config file (see load_config()); the AnkerWork M650 is the default.

Background mode (default) listens on ws://127.0.0.1:2395, where the Meet
browser extension (streamdeck-googlemeet, branch feat/mic-watcher-control)
connects while a Meet tab is open. While it is connected, the mic is
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
import fnmatch
import json
import math
import os
import shutil
import signal
import sys
import time
import tomllib
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

RATE = 16000
BLOCK_SAMPLES = RATE // 20          # 50 ms
ZERO_BLOCKS_FOR_MUTE = 3            # 150 ms of exact zeros
LIVE_BLOCKS_FOR_UNMUTE = 2          # 100 ms of signal
DEVICE_POLL_S = 2.0
READ_TIMEOUT_S = 1.0
COMMAND_CONFIRM_S = 3.0
FLOOR_DB = -90.0

DEFAULT_CONFIG = {
    # PipeWire node.name glob patterns of mics verified to send exact zeros
    # only when muted. Earlier patterns win if several mics are attached.
    "mics": ["alsa_input.usb-Actions_AnkerWork_M650_RX_*"],
    # Must match the second port in the Meet browser extension.
    "port": 2395,
}


def default_config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "mic-zero-watch" / "config.toml"


def load_config(path):
    """Read the TOML config; every key is optional and falls back to DEFAULT_CONFIG."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return config
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"mic-zero-watch: invalid config {path}: {e}")
    unknown = set(data) - set(DEFAULT_CONFIG)
    if unknown:
        sys.exit(f"mic-zero-watch: unknown key(s) in {path}: {', '.join(sorted(unknown))}")
    if "mics" in data:
        mics = data["mics"]
        if not (isinstance(mics, list) and mics and all(isinstance(m, str) for m in mics)):
            sys.exit(f"mic-zero-watch: 'mics' in {path} must be a non-empty list of strings")
        config["mics"] = mics
    if "port" in data:
        if not isinstance(data["port"], int) or not 0 < data["port"] < 65536:
            sys.exit(f"mic-zero-watch: 'port' in {path} must be a port number")
        config["port"] = data["port"]
    return config


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


async def find_mic(patterns):
    """Return the node.name of the first attached mic matching the patterns, or None."""
    proc = await asyncio.create_subprocess_exec(
        "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    try:
        objects = json.loads(out)
    except json.JSONDecodeError:
        return None
    names = [
        props.get("node.name", "")
        for obj in objects
        if (props := (obj.get("info") or {}).get("props") or {}).get("media.class") == "Audio/Source"
    ]
    for pattern in patterns:
        for name in names:
            if fnmatch.fnmatchcase(name, pattern):
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
            text = "\033[90mnone of the configured mics is connected\033[0m"
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


async def run(args, config):
    # systemd stops us with SIGTERM: unwind normally so pw-record is reaped.
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, asyncio.current_task().cancel)
    meet = Meet()
    server = None
    try:
        server = await serve(meet.handler, "127.0.0.1", config["port"])
    except OSError:
        if not args.live:
            log(f"port {config['port']} is already in use; is another instance running?")
            return 1
        meet = None  # live view only; the background service drives Meet
    live = Live(meet) if args.live else None
    if not args.live:
        log(f"listening for the Meet extension on 127.0.0.1:{config['port']}")

    try:
        while True:
            if not args.live and not meet.connected:
                # Nothing to do without Meet; don't hold the mic open.
                meet.changed.clear()
                await meet.changed.wait()
                continue
            mic = await find_mic([args.target] if args.target else config["mics"])
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
                        help="watch this PipeWire node instead of the configured mics")
    parser.add_argument("--config", metavar="PATH", type=Path, default=default_config_path(),
                        help="config file (default: %(default)s)")
    args = parser.parse_args()
    config = load_config(args.config)
    try:
        sys.exit(asyncio.run(run(args, config)))
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        if args.live:
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()
