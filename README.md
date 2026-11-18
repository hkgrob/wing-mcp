# WING MCP

An MCP server that lets Claude run a Behringer WING mixing console (the full-size WING, the Compact and the Rack) over your local network. You ask in plain language; Claude reads and writes the desk through tools that validate every change, read it back, and refuse anything that could lock you out or destroy a recording.

It covers the whole job, from building scenes to tuning the room:

- **Read and write anything** on the desk, by name, with every write checked and confirmed.
- **Show files:** dump any part of the desk to a text file, edit it or generate it from a script, and push it back as a background job with a per-node report.
- **Presets:** save a strip, an EQ or a bus by name and recall it, or recall it onto other channels to copy or move strips.
- **Live operation:** timed fades, a live feed of every change made at the desk, and opt-in scene recall.
- **Meters and gain:** live levels, the desk's 120-band RTA, and one-call gain staging that uses the preamp for coarse moves and the channel trim for fine ones.
- **Dynamics setup:** gate and compressor thresholds from measured levels.
- **USB audio analysis:** stream loudness to EBU R128, mic polarity and delay, precise feedback frequencies.
- **PA tuning:** dual-FFT measurement with coherence, multi-seat averaging, target curves, automatic EQ fitting, mains/subs alignment and room acoustics.
- **A web GUI** on the same engine, usable from a browser or an iPad.

Version 0.7.0. See [CHANGELOG.md](CHANGELOG.md).

This is a fork of [dwrightii/wing-mcp](https://github.com/dwrightii/wing-mcp) by Doug Wright, which built the transport, profile, validation and push engine for the WING Rack. This fork adds full-size and Compact support, macOS support, and everything from meters onward.

## Status

| Area | Tested on |
| --- | --- |
| Connection, profile build, reads, writes, batch writes, show-file push (including a five-channel strip move), meters, RTA, gain staging | Hardware: full-size WING, firmware 3.1.2, from macOS |
| Original transport, validation and push engine | Hardware: WING Rack, firmware 3.1-0, from Windows (upstream) |
| Fades, change feed, presets, auto-gate/compress, scene control, USB audio analysis, PA tuning | A simulated desk and synthetic audio with known answers; not yet on hardware |

The analysis code is checked against reference signals: EBU Tech 3341/3342 loudness test tones (within 0.1 LU), planted polarity and delay offsets, a hidden ringing tone, a simulated PA with known EQ, and a room with a known reverb time. Reports from other desks, firmware versions or Windows are welcome on [Issues](https://github.com/hkgrob/wing-mcp/issues).

## Requirements

- A WING on the same network as your computer. The desk's OSC is unauthenticated UDP, so there's no password to enter, and anything on your network that can reach the desk can control it.
- Claude Desktop.
- Python 3.10 or newer with the `mcp` package (`mcp<2`).
- Optional, for USB audio analysis: a USB cable from the WING to the computer, and the packages numpy, scipy and sounddevice. `wing_audio_check` can install them for you.
- Optional, for PA tuning: an omni measurement mic (Behringer ECM8000, dbx RTA-M or similar) on a spare input with phantom power.

## Install

### macOS

1. Install Python and the `mcp` package. Apple's built-in Python is too old.
   ```
   brew install python@3.12
   /opt/homebrew/bin/python3.12 -m pip install --break-system-packages "mcp<2"
   ```
2. Install the extension: double-click `wing-mcp-v0.7.0.mcpb`, or in Claude Desktop go to Settings → Extensions → Advanced settings → Install Extension. Remove any older version first.
3. The first time the server looks for the desk, macOS asks to let it find devices on your local network. Allow it. If you missed the prompt, it's under System Settings → Privacy & Security → Local Network.

The bundle starts through `server/launch.py`, which finds a Python 3.10+ that has `mcp` (Homebrew, python.org or pyenv), because Claude Desktop usually only sees Apple's `/usr/bin/python3`. To force a specific interpreter, run `launchctl setenv WING_MCP_PYTHON /path/to/python3` and restart Claude Desktop.

### Windows

Put `python` 3.10+ on PATH, run `python -m pip install "mcp<2"`, then install the `.mcpb` as above.

### First connection

1. Ask Claude to run `wing_list_consoles` and note your desk's name.
2. Ask it to `wing_connect` to that name.
3. **Full-size WING or Compact:** run `wing_generate_profile` once. It walks the desk (read-only, about 4 minutes in the background) and saves its own command profile to `~/.wing-mcp/profiles/<model>.json`, where reinstalling never touches it. Run it again after any firmware update. The bundle ships only the Rack's profile; until your desk has its own, writes are re-checked live against the desk.
4. For USB audio analysis: ask Claude to run `wing_audio_check` with install, restart the extension, and allow the macOS microphone prompt the first time audio is recorded. macOS treats the WING's USB input as a microphone.

### Permissions in Claude

Every tool is labelled read-only or write, so Claude's permission settings can approve each group once instead of every call. If you're in an organization, its admin settings may still require approval for tools that make changes, whatever the per-tool setting says. Tools that act in one call (gain staging, batch writes, fades) keep the number of approvals down.

## Models

| Desk | Reports as | Profile |
| --- | --- | --- |
| WING (full size) | `wing-fullsize` on firmware 3.1.2 (older docs: `ngc-full`, `wing-bk`) | Build with `wing_generate_profile` |
| WING Compact | `wing-compact` | Build with `wing_generate_profile` |
| WING Rack | `wing-rack` | Shipped (firmware 3.1-0) |

The three share firmware and nearly all of one address tree. They differ in hardware: local preamps (8 on the full-size desk, 24 on the Rack and Compact), GPIO, talkback options, and the surface's layer and user-control nodes. That's why each desk validates against its own walk.

## What to ask Claude

- "Check the EQ on every channel and suggest fixes for each instrument."
- "Gain check the pastor's mic while he talks, target −15."
- "Move channels 7 to 9 one to the left and put the Audio PC on channel 9."
- "Fade the Audio-PC down 12 dB over 5 seconds."
- "Save the Pastor strip as a preset, then put it on channel 12 too."
- "What changed on the desk in the last ten minutes, and who changed it?"
- "Measure the stream mix loudness for the whole service."
- "Are the snare top and bottom mics in polarity?"
- "Find what's ringing in the wedges."
- "Measure the PA at three seats with pink noise, average them and fit the main EQ to the house curve."

## Tools

### Connection and profile

| Tool | What it does |
| --- | --- |
| `verify_connection` | Server version, connected desk and model, the profile in use, and every profile available. |
| `wing_list_consoles` | Every WING answering on the network: name, address, model, firmware. |
| `wing_connect` | Connects by console name, never by address, and refuses a name two desks share. |
| `wing_generate_profile` | Walks the desk in the background and builds its own command profile. |
| `wing_find` | Searches the command list by words in the address (`talk`, `dyn thr`, `io in LCL g`), including controls of processing models not currently loaded. |
| `wing_models` | Every EQ (7), gate (14), compressor (19), filter (4) and FX (65) model and each one's controls. |

### Reading and writing

| Tool | What it does |
| --- | --- |
| `wing_get` | Reads nodes: each parameter's value, read-only flag and legal range, plus the node's children. |
| `wing_set` | Writes to one node. Whole-message validation (one bad value sends nothing), read-back after every write, and staged model changes: the model goes first, then its settings against the shape it actually loads. `dry_run` shows the messages. |
| `wing_set_many` | Many writes in one call, all validated before any is sent. |

### Show files and background jobs

| Tool | What it does |
| --- | --- |
| `wing_dump` | Reads a subtree to a show file. Large walks continue in the background, and anything the desk couldn't return is named in the file. |
| `wing_push_file` | Pushes a show file as a background job, with a per-node report, read-back on every node and up to three whole-push retries. Routing is applied before input settings, and a channel never writes its source's preamp or phantom. |
| `wing_push_status` / `wing_job_status` | Progress and results of a push, dump or profile walk. |

### Presets

| Tool | What it does |
| --- | --- |
| `wing_preset_save` | Saves any part of the desk ("/ch/11", "/bus/3", "/ch/1/eq") by name to your computer, never to the console library. |
| `wing_preset_list` | Saved presets. |
| `wing_preset_load` | Recalls a preset, optionally retargeted (`{"/ch/11": "/ch/12"}`), which is how strips are copied or moved. |

### Meters, RTA and gain

| Tool | What it does |
| --- | --- |
| `wing_meters` | Live input and output levels and gate/compressor gain reduction per strip, from the desk's native meter stream. |
| `wing_rta` | The desk's 120-band RTA on any strip, pre- or post-EQ, with stand-out bands. On music these are usually the notes being played; they mean resonance on a single mic. |
| `wing_gain_check` | Gain staging in one call: measure, adjust and re-measure until each channel is in range. The preamp takes coarse moves (0.5 dB steps) and the channel trim smooths in 0.1 dB steps; sources without a preamp use the trim. The fader can move the opposite way so the room level holds. |

### Live operation

| Tool | What it does |
| --- | --- |
| `wing_fade` | Timed moves of faders, sends or any level: absolute or relative, several at once, linear in dB or S-curved, in the background. |
| `wing_fade_status` / `wing_fade_stop` | Running fades; stop one where it is, or all of them. |
| `wing_watch` / `wing_changes` | Live change feed: every change on the desk, marked as made by this server or by someone else (surface, iPad, other apps). It uses the desk's single event subscription, so it's off until asked; it takes the subscription from Bitfocus Companion if you use it. |
| `wing_scene_status` | The open show, active scene and scene list. |
| `wing_scene` | Scene recall (GO, GOTAG, NEXT, PREV, GONEXT, GOPREV). Off unless `~/.wing-mcp/config.json` contains `{"scene_control": true}`, because a recall reconfigures the whole desk. |

### Dynamics setup

| Tool | What it does |
| --- | --- |
| `wing_auto_gate` | Two steps: measure while the sources are silent, then while they're played. The threshold goes just above the noise floor, or the tool reports too little separation. |
| `wing_auto_compress` | Compressor thresholds for a target gain reduction, from measured levels and the model's ratio. |

Both suggest by default and only write with `apply`.

### USB audio analysis

| Tool | What it does |
| --- | --- |
| `wing_audio_check` | Readiness: Python packages (with `install=true` to add them), the WING's USB audio device, and which USB outputs it will borrow. |
| `wing_loudness` | Loudness to ITU-R BS.1770-4 / EBU R128: integrated LUFS, momentary and short-term, loudness range and true peak, against your target, with advice. |
| `wing_loudness_monitor` / `wing_loudness_status` | A background loudness meter on the stream bus for a whole service. |
| `wing_polarity_check` | Arrival difference and polarity between two mics on one source; can set the channel's invert and input delay. |
| `wing_feedback_find` | Ringing frequencies to about 1.5 Hz, told apart from musical notes, with a suggested notch for each; can write one into a bus or main EQ. |

### PA tuning

| Tool | What it does |
| --- | --- |
| `wing_pa_measure` | Measures the PA at one seat: frequency response, phase, coherence, arrival time and room reverb, from programme material or the desk's own pink-noise oscillator. Supports mic calibration files. |
| `wing_pa_list` | Saved measurements. |
| `wing_pa_report` | Response against a target curve (flat, house, live or custom), trusted only where coherence is good, with the biggest problems. |
| `wing_pa_average` | Coherence-weighted average of several seats, plus how much the seats differ. |
| `wing_pa_eq` | Fits a parametric EQ toward the target (cuts preferred, boosts capped, cancellation dips ignored) and applies it to the main or matrix EQ, after saving a backup preset. |
| `wing_pa_align` | Delay and polarity between two parts of the PA (mains and subs, mains and fills); can set the output delay. |
| `wing_pa_room` | Room acoustics per octave (ISO 3382-1): EDT, T20, T30, C50, C80, D50, with a plain-language summary. |

## USB audio analysis

The WING is also a 48×48 USB audio interface. To analyse something, the server points spare USB outputs at it, records over USB, and puts the outputs back exactly as they were.

- **Which outputs it borrows:** the highest-numbered USB outputs currently routed to nothing, so a stream or recording feed on the low outputs is never touched. To choose them yourself, add `"usb_analysis_outs": [47, 48]` to `~/.wing-mcp/config.json`.
- **Sources you can name:** `main:1`, `bus:3`, `mtx:2` (stereo; add `L` or `R` for one side), `src:A.24` or `src:LCL.2` (a raw input), or `ch:40` (the raw input feeding that channel, before its processing).
- **Methods:** published standards implemented from their specifications. ITU-R BS.1770-4 and EBU R128 for loudness; the dual-channel FFT transfer function with coherence (the technique behind Smaart and Open Sound Meter); ISO 3382-1 for room acoustics; the RBJ Audio EQ Cookbook for filters.

### PA tuning workflow

1. Put the measurement mic on a spare input and give a channel its source, muted and off the mains. Tell Claude which input that is, and which spare channel to use for pink noise.
2. Place the mic at a typical seat, at ear height, away from walls.
3. Measure: `wing_pa_measure` with pink noise (or with music playing). Pink noise starts at −20 dB and fades in, but it's loud, so warn anyone in the room. The noise channel is set up for the test with all its sends off and is restored exactly afterwards.
4. Repeat at two or more other seats, then `wing_pa_average` them.
5. `wing_pa_report` against the target, then `wing_pa_eq` to fit and apply the main EQ.
6. Measure again to check. Use `wing_pa_align` for subs or fills, and `wing_pa_room` for the room's acoustics.

## The GUI

```
python3 server/wing_gui.py          # opens http://127.0.0.1:8765
python3 server/wing_gui.py --lan    # also reachable from an iPad on the same network
```

It uses only the Python standard library and calls the same functions as Claude's tools, so every check is identical.

| Tab | What's there |
| --- | --- |
| Node | Address-tree browser with an editor for each node; changed values are highlighted and every write is confirmed by read-back |
| Find | Command search |
| Dump / Push | Dumps, dry runs and pushes with live progress |
| Meters | Level table, RTA graph, gain check |
| Live | Fades, change feed, presets, scene status |
| PA & Audio | Loudness meter, PA measurement, response plot with target and coherence, EQ preview and apply, room table |
| Profile | Profile status and builds |

It runs as its own process, separate from Claude's, so don't start a push from both at once. `--lan` has no login; use it only on a network you trust.

## Command-line scripts

| Script | What it does |
| --- | --- |
| `server/wing_push.py` | Push a show file from Terminal, with `--dry-run`, `--only /ch/1` and `--limit`. |
| `server/wing_harvest.py` | Dump the post-show set of roots (or `--root` ones) to `outputs/`. |
| `server/wing_profile.py show` | List the profiles this install can load. |
| `server/wing_transport.py selftest` | Codec and parser self-test; `info` lists the consoles on the network. |
| `tools/build_model_catalog.py` | Rebuild the model catalog from libwing's property map. |

The scripts take `--console NAME` or `$WING_CONSOLE`.

## Configuration

Everything the server saves lives in `~/.wing-mcp/` (or `$WING_MCP_HOME`), outside the extension, so reinstalls never touch it:

| Path | Contents |
| --- | --- |
| `profiles/` | Your desk's command profile |
| `maps/` | The raw walks behind each profile |
| `dumps/` | Default location for `wing_dump` |
| `presets/` | Saved presets |
| `measurements/` | PA measurements and impulse responses |
| `config.json` | Settings: `{"scene_control": true, "usb_analysis_outs": [47, 48]}` |

Environment variables: `WING_MCP_HOME`, `WING_MCP_PYTHON` (interpreter to use on macOS), `WING_CONSOLE` (default console name for the scripts), `WING_AUDIO_DEVICE` (part of the USB audio device's name, if it isn't "WING"), `WING_SCENE_CONTROL=1`.

## Safety

- **Blocked completely, from every tool:** card format, recording deletes, recorder transport, scene recall (except through the opt-in `wing_scene`), console-wide save, the desk's network settings (IP, mask, gateway, DHCP), the TCP lock and OSC read-only. A bad write to any of these would lock you out until someone fixes it at the surface.
- **The desk's `OK` isn't trusted.** It means a write was received, not stored, so every write is read back and compared, allowing for the desk's own rounding.
- **Out-of-range values never leave the computer.** Every value is checked against the desk's own list of types and ranges first.
- **Connect by console name, never by address,** and two desks with the same name are refused.
- **The console's library is never touched.** Saving a scene is always done on the surface.
- **Anything the tools borrow is put back:** USB outputs for analysis, the pink-noise channel, the RTA source.
- **Batch and PA EQ changes save a backup preset first,** so they can be undone.

Full rules are in [docs/safety.md](docs/safety.md).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No console found | The desk is on and on the same network and subnet; Local Network permission for Claude (macOS); a firewall such as Little Snitch allowing UDP 2223 |
| Writes return OK but nothing moves | The desk's OSC remote lock (the read-back reports a mismatch) |
| Meters or RTA empty | Incoming UDP from the desk is allowed for the extension's Python (macOS firewall or Little Snitch) |
| Extension won't start on macOS | No Python 3.10+ with `mcp`: install it as above, or set `WING_MCP_PYTHON` |
| No WING USB audio device | The USB cable is on the desk's rear USB audio port; run `wing_audio_check` |
| A tool asks for approval every time | Your organization's admin settings may require approval for tools that make changes |
| Change feed empty, or Companion stops updating | The desk allows one event subscription at a time; turn the feed off when you're done |

## Project layout

| Path | Contents |
| --- | --- |
| `server/server.py` | The MCP tools |
| `server/wing_transport.py` | OSC codec, discovery, node reads and writes |
| `server/wing_native.py` | Native-protocol meters and RTA |
| `server/wing_profile.py` | Profiles, the writable set, validation |
| `server/wing_push_core.py` / `wing_push.py` | The staged push engine and its CLI |
| `server/wing_harvest.py` / `wing_jobs.py` | Tree walks and background jobs |
| `server/wing_live.py` | Fades and the change feed |
| `server/wing_audio.py` / `wing_dsp.py` | USB capture and routing; loudness, correlation, transfer function, room metrics, EQ fitting |
| `server/wing_catalog.py` / `model_catalog.json` | The processing-model catalog |
| `server/wing_gui.py` / `gui.html` | The web GUI |
| `server/launch.py` | Interpreter-finding launcher |

## Docs

- [docs/cowork-setup.md](docs/cowork-setup.md): running a desk from a Claude working folder.
- [docs/build-your-own-generators.md](docs/build-your-own-generators.md): having Claude build scene generators for your own bands.
- [docs/wing-file-format.md](docs/wing-file-format.md): the show file format.
- [docs/safety.md](docs/safety.md): the rules a fork keeps.

## The Android companion app

The WING has one analyzer, and whichever desk page is drawing an RTA claims it. [wing-rta-toggle](https://github.com/dwrightii/wing-rta-toggle) switches the desk's own RTA overlays off so a remote mixing app on a phone can hold the analyzer, and switches them back afterwards.

## Credits and license

MIT licensed. Original server by Doug Wright ([dwrightii/wing-mcp](https://github.com/dwrightii/wing-mcp)).

The model catalog is derived from [libwing](https://github.com/dannyfiresnake/libwing) (Apache-2.0); see `server/THIRD_PARTY_NOTICES.md`. Protocol details come from Patrick-Gilles Maillot's *WING Remote Protocols* and from the desk's own introspection.
