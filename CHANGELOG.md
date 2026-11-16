# Changelog

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
