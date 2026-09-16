# The show file format

A show file is the text form of a desk state. `wing_dump` writes it and `wing_push_file` reads it, so a dump is always a valid input to a push.

## The line

The file is UTF-8 text with one node per line. A line is an address, then a single TAB, then `key=value` pairs separated by TABs.

```
# Comment lines start with a hash and carry no address.
/ch/1	name=Vocal	mute=1	tags=#M2,#D4
/ch/2	name=Gtr DI	mute=1
/bus/1	name=Monitor A	busmono=1
```

The separator is a TAB rather than a comma for one reason: the desk's own node message separates pairs with commas, so a value containing a comma splits into a bogus key. `tags=#M2,#D4` above survives only in the tab form. Tabs never appear in WING values, so the tab form has no equivalent hole.

Older files used commas between pairs and still parse. A file is read as tab-separated if it contains a TAB and comma-separated otherwise, per line.

## Comments, blanks and errors

A blank line or a line whose first non-space character is `#` is skipped and carries no address. Two things are parse errors, reported with a line number:

- a line with no TAB in it
- an address that does not start with `/`

A pair with no `=` in it is reported as a problem on that line.

## Values with a space or a comma

A value carrying a space or a comma is written to its own leaf address as a single string argument, then read back, because the node `key=value` form strips spaces and splits on commas and returns `OK` either way. This is handled for you. It is worth knowing because it is why `name=Gtr DI` above is safe in a file and would not be safe as a raw node write.

## Partial and incomplete nodes

A node the file only partly specifies is written as far as the file goes. The keys the file gives are written and read back exactly as they would be if the file were complete, and the live parameters the file never mentioned come back on that node as `partial`, with a warning. Partial is a report, not a refusal.

One shape is withheld outright, reported as `incomplete` with nothing sent: a model-bearing node whose line names only the model, optionally plus `on`, and no parameters under it. Writing only the model would leave every parameter under that model at whatever the previous scene held, which reads as a clean write and is not one. Either specify the model's parameters or leave the node out of the file.

Two kinds of key are never demanded of a file, so they never appear in a `partial` list:

- anything whose name starts with `$`. These are surface mirrors and transients, not values a show file is expected to carry.
- anything the profile marks as policy-excluded, such as scene recall or card format. A parameter that could never legally be written is never asked for.

## Round trip

`wing_dump` writes only writable parameters with a non-null value, one line per node, in this format. Dump a subtree, edit the file, push it back, and the two ends agree on every rule above.
