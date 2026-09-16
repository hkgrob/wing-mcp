# Running a WING from a Claude working folder

Cowork is the Claude Desktop mode that works inside a folder on your computer: it reads and writes the files in that folder and runs the connectors you have installed. That means Claude can hold your desk's scene files, the scripts that render them and the notes about your desk in one place, and use this server to talk to the console.

For installing Claude Desktop and connecting a folder, see Anthropic's documentation at <https://support.claude.com>. This guide covers what to put in the folder once you have one.

## The working shape

Install the bundle first. Download `wing-rack-mcp-v0.3.1.mcpb` from Releases and install it in Claude Desktop, under Settings, then Connectors, then install from file. The README's five steps get you to a value coming back off channel 1; do that before anything here.

Then connect a working folder and make one project folder inside it for the desk. That folder holds four things.

**A rules file, `CLAUDE.md`.** These are the standing decisions Claude follows for this desk, written once so you do not restate them every session. They are decisions, not documentation. Three examples of the kind of line that belongs there:

- Every show file is rendered by a script in `generators/` and never edited by hand; a fix goes in the script, then the file is re-rendered.
- Confirm the console name before any write, and stop if more than one desk answers the discovery call.
- No writes to the desk between load-in and the end of the night. The build happens the day before.

Yours will be different. The test for a line is whether you would be annoyed to find it had been ignored.

**A state file, `state.md`.** What is true right now: which scene is loaded, which library slot it came from, the firmware the desk reports, the date of the last dump and where it landed. This one is edited in place rather than appended to, so it never grows into a log.

**A `generators/` subfolder** holding every script that renders a show file. One folder, so a session can find them without being told where to look.

**An `outputs/` subfolder** holding the rendered show files and the push reports. Keep the dump you took before a push next to the file you pushed.

## The loop

1. **Dump the desk.** Run `wing_dump` per channel and per family root and write each dump into `outputs/`. This is your baseline: the desk as it is, in the same format the pusher reads back. A large family cannot be returned in one reply, so dump channels individually rather than asking for the whole layer at once.
2. **Write the scene.** Edit a dump by hand for a small change, or render a fresh file from a generator for a whole band. Either way the file is the artifact.
3. **Push it.** `wing_push_file` starts a background job and hands back a job id. Poll `wing_push_status` with that id until the state is something other than running. Read the report file it names; the partial and incomplete counts are where a scene file's gaps show up, not the job state.
4. **Save it to the console library, on the surface.** The WING exposes no library save over OSC, so this step is yours, at the desk, by hand. Name the slot after the file you pushed.
5. **After the gig, dump again and diff** the new dump against the file you pushed. What changed is what you moved during the show.

## Why the loop is shaped this way

A full scene of about 3,000 nodes takes several minutes to push, because every node is written and read back. That is fine the day before a show and useless at a gig. A library recall on the surface is instant. So the pusher is a build tool, and the library is the gig tool: you push once, save the result into the library, and recall from the library at the venue. Push at the desk on show day and you are watching a progress counter while a band waits.

The diff at the end is what makes the whole thing worth the trouble. Every fader you moved and every filter you dialed in during the show is a difference between the file you pushed and the desk you are dumping. Read that list and you are deciding which of those changes were the room and which were the template being wrong. The ones that were the template go back into the generator, and the next band's file starts closer to right.
