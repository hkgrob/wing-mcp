# WING MCP

An MCP server that lets Claude control a Behringer WING over OSC on your local network: the full-size WING, the Compact and the Rack. It picks a command profile by the model the desk reports, and comes with a local web GUI that drives the same engine. It is for people who build their own scenes: read any node, write named parameters, dump a subtree to a text file, edit it, and push it back.

Version 0.7.0. See [CHANGELOG.md](CHANGELOG.md).

## What you need

A WING on the same LAN as your computer. Claude Desktop. Python 3.10 or newer with the `mcp` package (`mcp<2`) installed in it.

There is no credential to enter: the desk speaks unauthenticated UDP on the LAN. Anything on your network that can reach the desk can control it.

**macOS:** the bundle starts the server through `server/launch.py`, which hands off to a Python 3.10+ that has `mcp` installed (Homebrew, python.org or pyenv), because Claude Desktop's PATH usually only reaches Apple's older `/usr/bin/python3`. The easy route: `brew install python@3.12`, then `/opt/homebrew/bin/python3.12 -m pip install --break-system-packages "mcp<2"`. To force one interpreter, run `launchctl setenv WING_MCP_PYTHON /path/to/python3` and restart Claude Desktop. macOS asks for Local Network permission the first time; allow it, or discovery finds nothing.

**Windows:** `python` on PATH, 3.10 or newer, with `python -m pip install "mcp<2"`.

## Install

1. Install the interpreter and `mcp` as above.
2. Install `wing-mcp-v0.7.0.mcpb` in Claude Desktop: double-click the file, or go to Settings, then Extensions, then Advanced settings, then Install Extension.
3. Put the computer on the same LAN as the desk.
4. Ask Claude to run `wing_list_consoles` and note your desk's name.
5. Ask Claude to run `wing_connect` with that name, then `wing_get` on `/ch/1`.
6. **Full-size WING or Compact:** run `wing_generate_profile` once. The bundle ships only the Rack's profile; until your desk has its own, `wing_connect` says so and writes are re-checked live wherever the Rack profile would refuse them. The walk is read-only, runs in the background (poll `wing_job_status`), takes several minutes, and saves to `~/.wing-mcp/profiles/<model>.json`, where reinstalls never touch it. Do it again after any firmware update.

## Models

| Desk | Reports as | Profile |
| --- | --- | --- |
| WING (full size) | `wing-fullsize` on fw 3.1.2 (older docs: `ngc-full`, `wing-bk`) | build with `wing_generate_profile` |
| WING Compact | `wing-compact` | build with `wing_generate_profile` |
| WING Rack | `wing-rack` | shipped (firmware 3.1-0) |

The three share firmware and nearly all of one address tree. They differ in the hardware: the number of local preamps, GPIO, talkback options and the surface's layer and user-control nodes. That is why each desk validates against its own walk.

## Tools

| Tool | What it does |
| --- | --- |
| `verify_connection` | Health check. Returns the server version, the loaded profile's firmware and whether a desk is connected. |
| `wing_list_consoles` | Every WING answering on the network, with its name, address, model and firmware. |
| `wing_connect` | Connects by console name, never by address. On a shared network an address match can land on another desk. |
| `wing_get` | Reads one or more nodes: each parameter's value, read-only flag and legal range, plus the node's children. |
| `wing_set_many` | Several writes in one call, all validated before any is sent. |
| `wing_find` | Searches the profile for commands by words in their address (`talk`, `dyn thr`, `io in LCL g`), with type, range and whether each is writable. |
| `wing_set` | Writes named parameters to one node. Validation is whole-message, so one bad key sends nothing. Every write is read back. A model change plus that model's parameters is staged: model first, then the rest against the new shape. `dry_run` shows every message without sending. |
| `wing_dump` | Reads a subtree to a show file, child by child. Large walks continue in the background; anything the desk could not return is named in the file. Files default to `~/.wing-mcp/dumps/`. |
| `wing_push_file` | Pushes a show file as a background job and returns a job id straight away. Retries the whole push up to three attempts. |
| `wing_push_status` / `wing_job_status` | Polls a background job (push, dump or profile walk): state, progress, counts, and the report or result. |
| `wing_meters` | Live levels per strip: input and output peaks, gate and compressor gain reduction. Uses the desk's native meter stream: TCP 2222 out, UDP back to your computer, so the firewall must allow incoming UDP. |
| `wing_rta` | The desk's 120-band RTA on one strip, pre- or post-EQ, with resonances called out. |
| `wing_gain_check` | Soundcheck gain staging: measures each channel while it is played and suggests preamp gain, or applies it in capped steps. |
| `wing_models` | Every EQ, gate, dynamics, filter and FX model and its controls, including models not loaded on the desk. |
| `wing_fade` | Timed fader/send moves: absolute or relative, several at once, linear-in-dB or S-curve. `wing_fade_status`, `wing_fade_stop`. |
| `wing_watch` / `wing_changes` | Live change feed: what changed on the desk and whether this server or someone else did it. Uses the desk's single OSC event subscription, so it is off until asked. |
| `wing_preset_save` / `_list` / `_load` | Named presets of any part of the desk, saved on this computer; load can retarget onto other channels (copy or move strips). |
| `wing_auto_gate` / `wing_auto_compress` | Gate and compressor thresholds from measured levels. Suggest by default. |
| `wing_scene_status` / `wing_scene` | Show and scene status; scene recall (GO/NEXT/PREV/GOTAG) only when enabled in `~/.wing-mcp/config.json` with `{"scene_control": true}`. |
| `wing_audio_check` | Readiness for USB audio analysis; `install=true` installs numpy, scipy and sounddevice. |
| `wing_loudness` / `_monitor` / `_status` | Stream loudness to EBU R128: integrated LUFS, short-term, loudness range, true peak, against your target. |
| `wing_polarity_check` | Polarity and arrival difference between two mics on one source; can set invert and delay. |
| `wing_feedback_find` | Ringing frequencies to about 1.5 Hz with notch suggestions. |
| `wing_pa_measure` / `_report` / `_average` / `_eq` / `_align` / `_room` | PA tuning: dual-FFT measurement with coherence, multi-position averaging, target curves, EQ fit and apply, mains/subs alignment, room acoustics. |
| `wing_generate_profile` | Walks your desk in the background and builds its own profile. Read-only against the desk. |

Excluded from the writable set and unreachable from any tool: card format, recording deletes, scene recall, transport, console-wide save, the desk's network settings (IP, mask, gateway, DHCP), the TCP lock and OSC read-only. A bad write to any of those would lock you out until someone fixes it at the surface.

## USB audio analysis

Connect the WING to the computer by USB. Analysis borrows spare USB outputs - the highest-numbered ones routed to nothing, so a stream or recording feed on the low outputs is never touched - points them at what it needs, records, and puts them back. To choose them yourself, put `{"usb_analysis_outs": [47, 48]}` in `~/.wing-mcp/config.json`. Run `wing_audio_check` once with `install=true` to add numpy, scipy and sounddevice, then restart the extension. macOS asks once for microphone access - allow it.

For PA tuning, plug a measurement microphone (an omni such as the Behringer ECM8000, dbx RTA-M or similar, with phantom power) into a spare input and give its input as the mic: `src:A.24` or `ch:40`. Measure several seats, average them, fit the EQ to a target, measure again. Pink noise comes from the desk's own oscillator through a spare channel you name, set up for the test and restored afterwards.

The methods are published standards implemented from their specifications: ITU-R BS.1770-4 and EBU R128 (loudness), dual-channel FFT transfer function and coherence (the technique of Smaart and Open Sound Meter), ISO 3382-1 (room acoustics), RBJ Audio EQ Cookbook (filters).

## The GUI

```
python3 server/wing_gui.py          # opens http://127.0.0.1:8765
python3 server/wing_gui.py --lan    # also reachable from an iPad on the same network
```

It uses only the standard library, so there's nothing extra to install. It has a console scanner, an address-tree browser with an editor for each node (changed values are highlighted and every write is confirmed by read-back), command search, dumps, dry runs and pushes with live progress, and profile builds. Every button calls the same functions Claude's tools do, so the checks are identical. It runs as its own process: don't start a push from it while Claude is pushing. `--lan` has no login; use it only on a network you trust.

## The Android companion app

The WING has one analyzer, and whichever desk page is drawing an RTA claims it. [wing-rta-toggle](https://github.com/dwrightii/wing-rta-toggle) is a small Android app that switches the desk's own RTA overlays off so a remote mixing app on a phone can hold the analyzer, and switches them back when you are done. The APK is on its [Releases page](https://github.com/dwrightii/wing-rta-toggle/releases).

## Docs

- [docs/cowork-setup.md](docs/cowork-setup.md): running a desk from a Claude working folder.
- [docs/build-your-own-generators.md](docs/build-your-own-generators.md): having Claude build scene generators for your own bands.
- [docs/wing-file-format.md](docs/wing-file-format.md): the show file format.
- [docs/safety.md](docs/safety.md): the rules a fork keeps.

## Safety

The server never deletes from the console's library. A write is read back and compared, because the desk's `OK` acknowledges receipt and not storage. Connect by console name, never by address. Full rules in [docs/safety.md](docs/safety.md).

## Tested on

Hardware-tested: a WING Rack on firmware 3.1-0, driven from Windows. Version 0.4.0's full-size, Compact and macOS support is tested against a simulated desk and not yet on hardware; first reports are welcome. If you run it against a different WING, a different firmware or macOS, a first-time report on [Issues](https://github.com/dwrightii/wing-mcp/issues) is welcome.

MIT licensed. Built by Doug Wright. Model catalog derived from [libwing](https://github.com/dannyfiresnake/libwing) (Apache-2.0); see `server/THIRD_PARTY_NOTICES.md`.
