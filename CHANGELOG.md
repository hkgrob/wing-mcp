# Changelog

## 0.7.0 - 2026-11-17

USB audio analysis. With the WING connected to the computer by USB, the server borrows unused USB outputs (highest-numbered outputs routed to nothing, or those named in `config.json` as `usb_analysis_outs`), records, and restores them. Needs numpy, scipy and sounddevice - `wing_audio_check(install=true)` installs them.

- **Stream loudness** (`wing_loudness`, `wing_loudness_monitor`, `wing_loudness_status`): ITU-R BS.1770-4 / EBU R128 integrated, momentary, short-term, loudness range and true peak, against a target; a background meter for a whole service. Verified against EBU Tech 3341/3342 test signals.
- **Polarity and delay** (`wing_polarity_check`): arrival difference and polarity between two mics on one source; can set the channel's invert and input delay.
- **Feedback finder** (`wing_feedback_find`): ringing tones to about 1.5 Hz, told apart from moving musical notes, with notch suggestions; can write one into an EQ band.
- **PA tuning**: `wing_pa_measure` (dual-FFT H1 transfer function with coherence, arrival from the deconvolved impulse response, room reverberation; programme material or the desk's own pink-noise oscillator on a spare channel that is fully restored afterwards), `wing_pa_report` (third-octave deviation from flat / house / live / custom targets, trusted only where coherence says so), `wing_pa_average` (coherence-weighted multi-position average), `wing_pa_eq` (cut-preferring least-squares parametric fit, applied to the bus EQ with an automatic backup preset), `wing_pa_align` (mains/subs/fills delay and crossover polarity, applied to output delay), `wing_pa_room` (ISO 3382 EDT, T20, T30, C50, C80, D50). Mic calibration files are supported.
- GUI: a PA & Audio tab - loudness meter, measurement, response plot with target and coherence, EQ preview/apply, room table.

## 0.6.0 - 2026-11-17

New:
- **Model catalog** (`wing_models`, and `wing_find` now searches it): every EQ, gate, dynamics, filter and FX model's controls, including models not loaded anywhere on the desk. Dry runs predict a model change from it when no other node is on that model. Derived from libwing (Apache-2.0); see `server/THIRD_PARTY_NOTICES.md`. Reference only - the desk validates every write.
- **Fades** (`wing_fade`, `wing_fade_status`, `wing_fade_stop`): timed moves of faders, sends or any level, absolute or relative, linear-in-dB or S-curve, several targets at once, in the background.
- **Live change feed** (`wing_watch`, `wing_changes`): every change on the desk with where it came from - this server ("self") or the surface/other apps ("external"). Off until asked for, because the desk allows one event subscription at a time.
- **Presets** (`wing_preset_save`, `wing_preset_list`, `wing_preset_load`): save part of the desk by name and recall it, optionally retargeted onto other channels - which copies or moves strips.
- **Auto-gate** and **auto-compress** (`wing_auto_gate`, `wing_auto_compress`): thresholds from measured noise floor, playing level and the model's ratio; suggest by default.
- **Scene control** (`wing_scene_status`, `wing_scene`): GO / NEXT / PREV / GOTAG on the open show. Recall is off unless `~/.wing-mcp/config.json` has `{"scene_control": true}`.
- GUI: a Live tab with fades, the change feed, presets and scene status.

Fixed:
- The pusher never writes a channel's `$g`/`$vph` (mirrors of its source's preamp and phantom): written through a channel they hit whatever source the channel pointed at in that moment, which is the wrong one when the same file re-routes it. Each strip's `in/conn` is also processed before its `in/set`.
- Values longer than the desk's 16-character reply column (channel tags in several groups, scene names) were cut short on read, so a dump-then-push could truncate tags. The parser now locates each row's type by its shape, and a string filling the column is re-read on its own address.

## 0.5.3 - 2026-11-17

- `wing_gain_check` uses preamp and channel trim together: the preamp takes coarse moves (0.5 dB steps), the trim smooths in 0.1 dB steps. A correction under `fine_db` (3 dB) goes to the trim alone; the trim is held within ±`trim_limit_db` (6 dB), beyond which the preamp moves. The trim is per channel, so fine moves never touch other channels or direct outs sharing the input.

## 0.5.2 - 2026-11-17

- `wing_gain_check` loops by itself until every channel with signal is inside target ± tolerance (default −13 ± 1 dBFS), up to `max_passes`. It stops once in range instead of chasing the exact target, and halves a correction that reverses direction, so programme material doesn't ping-pong the gain. One call, one approval.
- New `wing_set_many`: a batch of writes validated together and sent in one call.
- Fixed: numeric ranges were not checked, so an out-of-range value (EQ gain −50 dB) passed validation. Now rejected before anything is sent.
- Fixed: values with a leading plus sign (`+0.1`, `+7.0`), which the desk itself writes in dumps, were rejected as not numbers, so a pushed dump silently skipped them.

## 0.5.1 - 2026-11-17

- Tools are annotated read-only or write, so Claude's permission settings can approve each group once.
- `wing_gain_check` uses the channel trim when a source has no preamp (aux line inputs, USB, modules), moves the fader the opposite way so the room level holds, steps up to 6 dB per run, and reports `converged`.
- `wing_meters` returns one short row per strip and lists silent strips separately; `detail=true` for every meter.
- `wing_rta` explains that stand-out bands on music are usually notes, not resonances.

## 0.5.0 - 2026-11-17

- New `wing_meters`: live input/output levels and gate/compressor gain reduction per strip, from the WING native meter stream (TCP 2222 out, UDP back).
- New `wing_rta`: the desk's 120-band RTA on any strip, pre- or post-EQ, with stand-out resonances; restores the RTA setting afterwards.
- New `wing_gain_check`: measures input peaks at soundcheck and suggests preamp gain, or applies it capped per step.
- GUI: Meters tab with levels, RTA graph and gain check.
- The full-size WING on firmware 3.1.2 reports `wing-fullsize`; it is now named as such.

## 0.4.0 - 2026-11-16

Full-size WING, Compact and macOS support, a web GUI, and fixes to the write path.

- **Models.** The profile is chosen by the model the desk reports (`ngc-full`, `wing-compact`, `wing-rack`). Profiles you build live in `~/.wing-mcp/profiles/` (or `$WING_MCP_HOME`) and survive reinstalls. `wing_connect` and `verify_connection` say when the profile in use was walked on a different model.
- **macOS.** The manifest lists `darwin`; `server/launch_mac.sh` finds a Python 3.10+ that has `mcp`. Discovery broadcasts on each interface's real broadcast address, not just a /24 guess.
- **Fixed: correct writes reported as failed.** A node mixing a value with a space and a number (`name=Lead Vox, fdr=0`) failed its read-back because the desk returns `0.0`. Read-backs now compare on the desk's step scale.
- **Fixed: `wing_set` trusted the desk's OK.** Every `wing_set` is now read back and compared.
- **Fixed: error replies counted as success.** A reply other than `OK` (`VALUE ERROR`, `NODE NOT FOUND`, …) is now a rejection.
- **Fixed: `wing_set` model changes.** `mdl` plus that model's parameters is staged the way the pusher does it.
- **Fixed: `wing_connect` during a job** closed the job's socket; it's now refused while a job runs.
- **Fixed: duplicate console names** were resolved to the first desk; now refused.
- **Fixed: dumps and profile walks lost whole subtrees on overflow** without saying so; they now fall back to names-only and record what was lost.
- `wing_dump` and `wing_generate_profile` run as background jobs; new `wing_job_status` polls any job. `wing_generate_profile` no longer blocks for half an hour.
- New `wing_find` searches the profile for commands.
- New exclusions: desk network settings, `tcplock`, OSC read-only.
- The `clink=0` warning covers aux inputs as well as channels.
- CLI scripts take `--console` or `$WING_CONSOLE` instead of a hard-coded name.
- New `server/wing_gui.py`: a local web GUI over the same engine.

## 0.3.1 - 2026-09-16

First public release.
