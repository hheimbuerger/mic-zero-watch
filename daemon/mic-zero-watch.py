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
MODE_POLL_S = 0.25
VOXTYPE_TIMEOUT_S = 5.0

STATE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "mic-zero-watch"
# Hands-free dictation switch: "on" or "off". Runtime dir, so it's off after a reboot.
MODE_FILE = STATE_DIR / "dictation"
# What the running service is doing, for the bar widget. Absent while it's not running.
STATUS_FILE = STATE_DIR / "status.json"

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

    def __init__(self, wake):
        self.connections = set()
        self.muted = None           # Meet's mic state; None until reported
        self.ours = False           # Meet is muted because we muted it
        self.pending = None         # (expected_state, deadline) after our command
        self.tx_muted = False       # transmitter state, as last reported
        self.changed = wake

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


class Dictation:
    """Hands-free dictation: the transmitter's mute button drives voxtype.

    While the mode is on and no Meet tab is connected, unmuting the transmitter
    starts a voxtype recording with smart auto-submit (ending with "submit"
    presses Enter), and muting it stops the recording, which pastes the text.
    Like the Meet link, it acts only on transmitter changes, and it only ever
    stops or cancels a recording it started itself.
    """

    def __init__(self, wake):
        self.wake = wake
        self.enabled = False
        self.recording = False      # a voxtype recording we started is running
        self._mode_mtime = None

    def poll_mode(self):
        """Re-read MODE_FILE if it changed; returns True if the mode flipped."""
        try:
            mtime = MODE_FILE.stat().st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self._mode_mtime:
            return False
        self._mode_mtime = mtime
        enabled = mtime is not None and MODE_FILE.read_text().strip() == "on"
        if enabled == self.enabled:
            return False
        self.enabled = enabled
        log(f"hands-free dictation {'on' if enabled else 'off'}")
        self.wake.set()
        return True

    async def _voxtype(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "voxtype", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), VOXTYPE_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            log(f"voxtype {' '.join(args)} timed out")
            return None
        return out.decode().strip()

    async def set_transmitter(self, tx_muted, meet_connected):
        """Called once per transmitter change."""
        if not self.enabled or meet_connected:
            return
        if not tx_muted:
            if self.recording:
                return
            status = await self._voxtype("status")
            if status != "idle":
                # Hotkey dictation or a transcription is running; leave it alone.
                log(f"transmitter unmuted, but voxtype is {status or 'unreachable'}; not starting")
                return
            await self._voxtype("record", "start", "--smart-auto-submit")
            self.recording = True
            log("dictation started")
        elif self.recording:
            self.recording = False
            await self._voxtype("record", "stop")
            log("dictation stopped, transcribing")

    def disable(self, reason):
        """Switch the mode off on the user's behalf; they switch it on again."""
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text("off\n")
        self._mode_mtime = MODE_FILE.stat().st_mtime_ns
        self.enabled = False
        log(f"hands-free dictation off ({reason})")
        self.wake.set()

    async def cancel(self, reason):
        if self.recording:
            self.recording = False
            await self._voxtype("record", "cancel")
            log(f"dictation cancelled ({reason})")


class Status:
    """Publishes STATUS_FILE for the bar widget, rewriting it only on changes."""

    def __init__(self):
        self.last = None

    def publish(self, **state):
        if state == self.last:
            return
        self.last = state
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATUS_FILE)

    def clear(self):
        STATUS_FILE.unlink(missing_ok=True)


async def watch_mic(mic, meet, dictation, live, publish):
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

    def wanted():
        return live or meet.connected or (dictation and dictation.enabled)

    try:
        while wanted():
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
            if dictation:
                if dictation.recording and (meet.connected or not dictation.enabled):
                    await dictation.cancel("Meet opened" if meet.connected else "mode switched off")
                elif changed:
                    await dictation.set_transmitter(tx_muted, meet.connected)
            publish(mic=mic, transmitter="muted" if tx_muted else "live")
            if live:
                live.show(mic, peak, "muted" if tx_muted else "zero" if peak == 0 else "live")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
        if dictation:
            await dictation.cancel("stopped watching the mic")


async def poll_mode(dictation):
    while True:
        dictation.poll_mode()
        await asyncio.sleep(MODE_POLL_S)


async def run(args, config):
    # systemd stops us with SIGTERM: unwind normally so pw-record is reaped.
    asyncio.get_running_loop().add_signal_handler(
        signal.SIGTERM, asyncio.current_task().cancel)
    wake = asyncio.Event()
    meet = Meet(wake)
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

    # Only the instance holding the port drives voxtype and reports status.
    dictation = Dictation(wake) if meet else None
    status = Status() if meet else None
    poller = asyncio.create_task(poll_mode(dictation)) if dictation else None

    connected = False  # a configured mic is attached, as of the last check

    def publish(mic=None, transmitter=None):
        if status:
            status.publish(dictation=dictation.enabled, recording=dictation.recording,
                           meet=meet.connected, connected=connected or mic is not None,
                           mic=mic, transmitter=transmitter)

    try:
        while True:
            # Cleared before looking around, so a wake during the lookup isn't lost.
            wake.clear()
            mic = await find_mic([args.target] if args.target else config["mics"])
            connected = mic is not None
            if mic is None and dictation and dictation.enabled:
                # Never look armed without a mic that can arm it: switch off,
                # and the user switches on again once the mic is back.
                dictation.disable("no configured mic connected")
            publish()
            idle = not args.live and not meet.connected and not dictation.enabled
            if mic is None or idle:
                # Nothing to watch; don't hold the mic open, but keep checking
                # whether it's attached so the bar widget can show it.
                if live:
                    live.show(None, 0, "live")
                try:
                    await asyncio.wait_for(wake.wait(), DEVICE_POLL_S)
                except asyncio.TimeoutError:
                    pass
                continue
            if not args.live:
                log(f"watching {mic}")
            await watch_mic(mic, meet, dictation, live, publish)
    finally:
        if poller:
            poller.cancel()
        if status:
            status.clear()
        if server:
            server.close()


def set_mode(value):
    """Write the hands-free dictation switch; the running service picks it up."""
    try:
        current = MODE_FILE.read_text().strip() == "on"
    except FileNotFoundError:
        current = False
    enabled = (not current) if value == "toggle" else value == "on"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text("on\n" if enabled else "off\n")
    print(f"hands-free dictation {'on' if enabled else 'off'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true",
                        help="show a one-line live view instead of logging")
    parser.add_argument("--target", metavar="NODE_NAME",
                        help="watch this PipeWire node instead of the configured mics")
    parser.add_argument("--config", metavar="PATH", type=Path, default=default_config_path(),
                        help="config file (default: %(default)s)")
    parser.add_argument("--dictation", choices=["on", "off", "toggle"],
                        help="switch hands-free dictation for the running service, then exit")
    args = parser.parse_args()
    if args.dictation:
        set_mode(args.dictation)
        return
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
