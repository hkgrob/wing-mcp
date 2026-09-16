# WING Rack MCP

An MCP server that lets Claude control a Behringer WING over OSC on your local network. It talks to the WING Rack, the WING Compact and the full WING. It is for people who build their own scenes: read any node, write named parameters, dump a subtree to a text file, edit it, and push it back.

Version 0.3.1 is the first public release.

## What you need

A WING on the same LAN as your computer. Claude Desktop. Python 3.12 or newer on PATH.

There is no credential to enter: the desk speaks unauthenticated UDP on the LAN. Anything on your network that can reach the desk can control it.

Windows is the tested platform. On macOS, `python` may be absent; if it is, edit `mcp_config.command` in `manifest.json` inside the bundle to `python3` before you install. Reports from macOS are welcome.

## Install in five steps

1. Confirm your interpreter and install the one dependency. Run `python --version` and check it reads 3.12 or newer, then `python -m pip install "mcp<2"`. Claude Desktop launches the server with whatever `python` is on your PATH, so the package has to be in that interpreter.
2. Download `wing-rack-mcp-v0.3.1.mcpb` from Releases and install it in Claude Desktop, under Settings, then Connectors, then install from file. The connector appears as `WING Rack (v0.3.1)`.
3. Put the computer on the same LAN as the desk.
4. Ask Claude to run `wing_list_consoles`. It prints every WING answering on the network. Note the name of yours.
5. Ask Claude to run `wing_connect` with that name, then `wing_get` on `/ch/1`. A channel of values coming back means you are done.

## Tools

| Tool | What it does |
| --- | --- |
| `verify_connection` | Health check. Returns the server version, the loaded profile's firmware and whether a desk is connected. |
| `wing_list_consoles` | Every WING answering on the network, with its name, address, model and firmware. |
| `wing_connect` | Connects by console name, never by address. On a shared network an address match can land on another desk. |
| `wing_get` | Reads one or more nodes: each parameter's value, read-only flag and legal range, plus the node's children. |
| `wing_set` | Writes named parameters to one node as a single message. Validation is whole-message, so one bad key sends nothing. `dry_run` shows the payload without sending. |
| `wing_dump` | Reads a subtree to a show file, walking child by child, because the desk cannot return a family root such as `/ch` in one reply. |
| `wing_push_file` | Pushes a show file as a background job and returns a job id straight away. Retries the whole push up to three attempts. |
| `wing_push_status` | Polls a push job: state, attempt, nodes done against total, elapsed seconds, failure, partial and incomplete counts, and the report file's path. |
| `wing_generate_profile` | Re-walks your own desk and rebuilds the command profile. Read-only against the desk. |

The shipped command profile was generated on firmware 3.1-0. Regenerate it with `wing_generate_profile` after any firmware change, so the types and ranges the server validates against match your desk.

Fifteen destructive addresses are excluded from the writable set and unreachable from any tool: card format, recording deletes, scene recall, transport and console-wide save.

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

One desk, one platform: a WING Rack on firmware 3.1-0, driven from Windows. If you run it against a different WING, a different firmware or macOS, a first-time report on [Issues](https://github.com/dwrightii/wing-mcp/issues) is welcome.

MIT licensed. Built by Doug Wright.
