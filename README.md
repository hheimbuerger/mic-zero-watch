# mic-zero-watch

Detects when a wireless mic's transmitter is muted or unmuted, and turns that into two features:

- **Meet link:** the transmitter's mute button mutes and unmutes your mic in Google Meet.
- **Hands-free dictation:** the transmitter's mute button starts and stops dictation with [voxtype](https://github.com/peteonrails/voxtype). Switch it on and off from an Omarchy bar widget.

Some wireless mics never tell the computer that their transmitter's mute button was pressed. The AnkerWork M650 is one: no HID report, no USB-audio status interrupt, nothing on any endpoint (verified with usbmon). But a muted transmitter makes the receiver send exact digital zeros, while an unmuted mic always carries some room noise, around -62 to -66 dBFS even in a silent room. So three consecutive 50 ms blocks of all-zero samples mean "muted", and two blocks with signal mean "unmuted" again. A switched-off transmitter presumably looks the same.

- [`daemon/`](daemon/): the watcher itself (`mic-zero-watch.py`), its systemd user service and an example config.
- [`omarchy-plugin/`](omarchy-plugin/): the optional bar widget for Omarchy's shell.

## Meet link

In the background (the default mode), the script listens on `ws://127.0.0.1:2395`. The Meet browser extension from [streamdeck-googlemeet](https://github.com/ChrisRegado/streamdeck-googlemeet), branch `feat/mic-watcher-control`, connects there while a Meet tab is open, alongside the Stream Deck plugin on 2394. While it's connected, the mic is watched:

- **Transmitter muted:** Meet's mic is muted, if it was on, and the mute is remembered as ours.
- **Transmitter unmuted:** Meet's mic is unmuted, but only if that mute is still ours.

The script acts only at the moment the transmitter changes, never on its steady state, so between two presses of the transmitter's button Meet is fully under manual control: unmuting Meet by hand while the transmitter is muted sticks. Any Meet mic change that didn't come from the script (a Stream Deck press, a click in Meet, Ctrl+D) also drops the "ours" mark, so the script never undoes a mute it didn't cause. If Meet connects while the transmitter is already muted, Meet is muted once as soon as it reports its state. While no Meet tab is connected, the mic isn't opened at all.

The mic is read through PipeWire with `pw-record` as an ordinary shared stream, so voxtype, Meet and anything else keep working. It never falls back to another mic if the receiver disappears.

## Hands-free dictation

A switch, off by default, that turns the transmitter's mute button into a dictation button for voxtype. While it's on and no Meet tab is connected:

- **Transmitter unmuted:** starts `voxtype record start --smart-auto-submit`.
- **Transmitter muted:** stops the recording, and voxtype pastes the text. If the dictation ended with the word "submit", voxtype removes it and presses Enter.

Like the Meet link, it acts only when the transmitter changes, so switching the mode on while the transmitter is already unmuted doesn't start a recording until the next mute and unmute. It only ever stops a recording it started itself: if voxtype is already busy, for example with a hotkey dictation, unmuting starts nothing. A Meet tab always wins: while one is connected, dictation is paused, and a recording in progress is cancelled when Meet connects. Switching the mode off mid-recording cancels the recording, so nothing is pasted. The mode also switches itself off whenever none of the configured mics is connected, including when you switch it on without one; switch it on again once the mic is back.

Switch it with the bar widget below, or from a terminal:

```sh
mic-zero-watch --dictation on|off|toggle
```

The switch is the file `$XDG_RUNTIME_DIR/mic-zero-watch/dictation`, so it's off again after every reboot.

## Omarchy bar widget

`omarchy-plugin/de.heimbuerger.mic-zero-watch` is a bar widget for [Omarchy](https://omarchy.org/)'s shell. Clicking it toggles hands-free dictation; right-clicking opens voxtype's settings (`omarchy-voxtype-config`). The mic icon's color is the switch, and a small badge next to it shows the substate:

| Icon | Meaning |
|---|---|
| mic, alert color, hollow circle badge | armed: the next unmute of the transmitter starts dictating |
| mic, alert color, filled circle badge | recording |
| mic, grey, video badge | on, but paused while a Meet tab is connected |
| mic, grey, no badge | off |
| crossed-out mic, grey | no configured mic is connected, or the service isn't running |

The widget reads `$XDG_RUNTIME_DIR/mic-zero-watch/status.json`, which the service keeps up to date while it runs and removes when it stops.

voxtype's own recording icon in Omarchy's indicators widget becomes redundant next to this widget and voxtype's on-screen overlay, and the widget's right-click replaces its shortcut to the settings. To hide it, give `omarchy.indicators` an `items` list without `Dictation` in `~/.config/omarchy/shell.json`:

```json
{ "id": "omarchy.indicators", "items": ["ScreenRecording", "Reminder", "NightLight", "Dnd", "StayAwake"] }
```

## Configuration

Optional, in `~/.config/mic-zero-watch/config.toml` (or under `$XDG_CONFIG_HOME`); see [`daemon/config.example.toml`](daemon/config.example.toml). Without a config file, the AnkerWork M650 receiver is watched on port 2395.

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
mic-zero-watch --dictation toggle     # switch hands-free dictation (on|off|toggle) and exit
```

In the live view, `Meet: muted*` means Meet is muted because of the transmitter, so unmuting the transmitter will unmute Meet. `Meet: n/a` means the background service already holds the port, and the live view only watches.

Requires `uv` (the script declares its one dependency, `websockets`, inline) and PipeWire's `pw-record`/`pw-dump`.

## Install

```sh
# From the root of your clone:
ln -s "$PWD"/daemon/mic-zero-watch.py ~/.local/bin/mic-zero-watch
systemctl --user link "$PWD"/daemon/mic-zero-watch.service
systemctl --user enable --now mic-zero-watch.service
journalctl --user -u mic-zero-watch -f

# Optional: the Omarchy bar widget
ln -s "$PWD"/omarchy-plugin/de.heimbuerger.mic-zero-watch ~/.config/omarchy/plugins/
omarchy-shell shell rescanPlugins
omarchy bar put de.heimbuerger.mic-zero-watch --after omarchy.indicators
```

Hands-free dictation needs voxtype's daemon running; the Meet link doesn't.
