# mic-zero-watch

Detects when a wireless mic's transmitter is muted, and mirrors that mute into Google Meet.

Some wireless mics never tell the computer that their transmitter's mute button was pressed. The AnkerWork M650 is one: no HID report, no USB-audio status interrupt, nothing on any endpoint (verified with usbmon). But a muted transmitter makes the receiver send exact digital zeros, while an unmuted mic always carries some room noise, around -62 to -66 dBFS even in a silent room. So three consecutive 50 ms blocks of all-zero samples mean "muted", and two blocks with signal mean "unmuted" again. A switched-off transmitter presumably looks the same.

## Meet link

In the background (the default mode), the script listens on `ws://127.0.0.1:2395`. The Meet browser extension from [streamdeck-googlemeet](https://github.com/ChrisRegado/streamdeck-googlemeet), branch `feat/mic-watcher-control`, connects there while a Meet tab is open, alongside the Stream Deck plugin on 2394. While it's connected, the mic is watched:

- **Transmitter muted:** Meet's mic is muted, if it was on, and the mute is remembered as ours.
- **Transmitter unmuted:** Meet's mic is unmuted, but only if that mute is still ours.

The script acts only at the moment the transmitter changes, never on its steady state, so between two presses of the transmitter's button Meet is fully under manual control: unmuting Meet by hand while the transmitter is muted sticks. Any Meet mic change that didn't come from the script (a Stream Deck press, a click in Meet, Ctrl+D) also drops the "ours" mark, so the script never undoes a mute it didn't cause. If Meet connects while the transmitter is already muted, Meet is muted once as soon as it reports its state. While no Meet tab is connected, the mic isn't opened at all.

The mic is read through PipeWire with `pw-record` as an ordinary shared stream, so voxtype, Meet and anything else keep working. It never falls back to another mic if the receiver disappears.

## Configuration

Optional, in `~/.config/mic-zero-watch/config.toml` (or under `$XDG_CONFIG_HOME`); see [`config.example.toml`](config.example.toml). Without a config file, the AnkerWork M650 receiver is watched on port 2395.

- **`mics`:** PipeWire `node.name` glob patterns of the mics to watch. If several matching mics are attached, earlier patterns win.
- **`port`:** the port the Meet extension connects to. It must match the extension.

### Does my mic qualify?

Only list mics that send exact zeros when muted and never otherwise. Many mics with a built-in noise gate also send exact zeros whenever you're quiet; on those, every pause in your speech would mute Meet. To check a mic, watch it with the live view:

```sh
mic-zero-watch --live --target <node.name>
```

- **Unmuted, sitting silently** for a minute: it must stay green (`live`) the whole time, never `MUTED`.
- **Muted:** it must switch to `MUTED / NO SIGNAL` within a fraction of a second, and stay there.

## Usage

```sh
mic-zero-watch            # background mode, logs state changes
mic-zero-watch --live     # one-line live view: level, mute detection, Meet state
mic-zero-watch --target <node.name>   # watch this PipeWire source instead of the configured mics
mic-zero-watch --config <path>        # use another config file
```

In the live view, `Meet: muted*` means Meet is muted because of the transmitter, so unmuting the transmitter will unmute Meet. `Meet: n/a` means the background service already holds the port, and the live view only watches.

Requires `uv` (the script declares its one dependency, `websockets`, inline) and PipeWire's `pw-record`/`pw-dump`.

## Install

```sh
# From the root of your clone:
ln -s "$PWD"/mic-zero-watch.py ~/.local/bin/mic-zero-watch
systemctl --user link "$PWD"/mic-zero-watch.service
systemctl --user enable --now mic-zero-watch.service
journalctl --user -u mic-zero-watch -f
```
