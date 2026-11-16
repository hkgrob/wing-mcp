# Safety rules

Five rules this server keeps. They are here so that a fork keeps them too. Four of them exist because something on the desk lies to you in a specific way, and one exists because a network does.

## Never expose a tool that deletes from the console's library

Enforcement is omission from the tool surface, not a caution in a docstring. A rule that lives in prose is a rule a model can talk itself past; a tool that does not exist cannot be called. The WING's OSC surface carries no library save at all, so this server also cannot overwrite or remove a stored show, scene, preset or snippet.

The destructive and lock-out addresses are excluded from the writable set for the same reason: card format, recording session and marker deletes, scene recall, transport and console-wide save. Nothing on this surface reaches them.

## Never overwrite an occupied library slot

Read the slot first and refuse if something is in it. Creating a new entry is fine. The slot you are about to write is the only copy of a show somebody built, and there is no undo on a console.

## The desk's `OK` is not proof a write landed

It acknowledges receipt, not storage. The node `key=value` form strips spaces from a value and splits it on commas, and returns `OK` either way, so a name written that way can come back mangled with nothing in the reply to say so. Any value carrying a space or a comma goes to its own leaf address, and every node is read back and compared against what was sent.

## A value read back over OSC is not proof the surface displays it

The `$`-prefixed mirror is what the console actually renders, and it can disagree with the value that was written. A channel written with a name but without `clink=0` is the standard case: the name stores correctly, reads back correctly, and never appears on the desk, because the channel is rendering its input source's identity instead. Any check that a write is visible compares the plain parameter against its `$` mirror.

## Connect by console name, never by IP

Discovery returns every WING answering on the network, each with its name. Match on the name. Festival networks reassign addresses, and an address that was your desk this morning can be somebody else's desk this afternoon; a name match fails safely, while an address match writes a full scene into a stranger's console.

## The command profile

The types and ranges every write is validated against come from the desk's own `,s "?"` introspection, walked node by node and stored. They are never transcribed from the protocol documentation, because the document describes the protocol and the profile has to describe your desk. If you change firmware, regenerate the profile with `wing_generate_profile` before you write anything.
