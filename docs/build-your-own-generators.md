Paste this file and `wing-file-format.md` together into a Claude session, then describe your band.

# Building scene generators for your own bands

## What a generator is

A generator is a script that takes a declaration of who plays what and renders a complete show file for one band. The declaration is the only thing you edit between bands; the script holds the rules that are true of your desk whoever is standing on stage. When something is wrong you fix the script and re-render, never the file, because a hand-edited file is a fix that exists for one show.

Keep the generator keyed by instrument and role rather than by channel number. Lineups change per date: five pieces one night and nine the next, two trumpets instead of a trumpet and a clarinet, a substitute drummer with a different kit. Every per-channel setting is really a property of an instrument, and the rules that look like channel rules are relationships between roles. "The bass keys off the kick" is the rule; whichever channels those two land on is a detail the declaration resolves for one date. Binding instruments to numbers belongs in a separate layer that maps this gig's lineup onto channels, not in the template.

## The desk is its own reference

Nothing in these two documents lists the address tree, the key names or the model catalogs, because the desk answers all of that live and the answer is right for your firmware. `wing_get` on any node returns every parameter with its current value, its read-only flag and its legal type or range, plus the node's children, so `wing_get` on `/` gives the roots and `wing_get` on a root gives its members. A dynamics or effects node reports its current model, and its parameter set is whatever `wing_get` returns after that model is set, so the full parameter list for any model is one `wing_set` of the model on a spare slot followed by one `wing_get`. `wing_dump` per root writes every writable parameter of every node under it, touched or not, so factory defaults appear in a dump and the byte-identical tell below is visible there.

The command profile is the server's stored copy of that introspection: every node, every parameter, its type and range, walked once with `wing_generate_profile`. Every write is validated against it, and the policy-excluded parameters (scene recall, card format, transport, console-wide save) live in it. The bundle ships a profile for firmware 3.1-0; regenerate it on your own desk before the first push.

## Finding out what your own desk already knows

Do not start from a blank file. Start from your desk.

Dump the desk after a show that went well. Dump it again after a different show with the same instruments in a different room. Then intersect the two. What is the same across both is a property of the instrument and belongs in the template. What differs is the room or that night, and belongs nowhere permanent.

One caveat. Harvesting twice separates the instrument from the room, but it does not separate either from what the model shipped with. On a 28-parameter effect, most of what sits on the desk was never touched by anybody. The tells are two instances of a model differing on only one or two parameters, and two nodes carrying byte-identical values. There is no cutoff that decides this for you; the question to ask before a value goes into a template is whether anyone remembers choosing it.

## The questions a template has to answer

Answer these for your own desk. A generator makes these decisions on every render, and one that has not decided them is deciding them by accident.

1. What state do channels, buses, mains and matrix load in? Give each layer an explicit answer rather than letting the previous scene decide.
2. Do channels load muted? If some do and some do not, what is the rule that tells them apart?
3. Which dynamics load engaged, which load in circuit but transparent, and which load off? A protection limiter that loads unset protects nothing; a bus compressor that loads hard can crush a show before a note reaches the room.
4. Which mute groups exist, what is in each, and which stay unassigned? Membership is written from the member's own side: a strip's `tags` string carries `#M<n>` for mute group n and `#D<n>` for DCA n, and `/mgrp/<n>` holds only the group's name and mute state. Buses and mains take tags the same way channels do.
5. What is the talkback source, and which buses does it reach? `/cfg/talk` carries the assignment and the per-button destination maps; `wing_get` on it shows the options your firmware offers.
6. What is the subgroup structure, and where do effects returns land?
7. What order do the monitor buses sit in, and what sits above them?
8. What does a push clear, and what must it leave alone? Routing and source labels are usually cleared. An internal player feeding a spare input and the talkback microphone's input are usually left alone, because clearing them breaks something that has nothing to do with the band.
9. How do channel labels stay visible after you patch by hand? Write `clink=0` on any channel the push names. With `clink=1` the channel renders its input source's identity instead, so the name stores correctly over OSC and never appears on the console.

## The one rule about dynamics

A dynamics stage is either fully specified or fully neutral. Never a model name with a partial parameter set.

Name a model and stop, and every parameter under that model keeps whatever the last scene left there. The write reports clean. The pusher withholds that exact shape, a line carrying `mdl` alone or `mdl` plus `on`, and writes the rest of the file. So a generator has two legal outputs for a dynamics node: the model with every one of its parameters written, or the node left out of the file. "Neutral" means the first form: the model and its complete parameter set, with `on=0` or with values that do nothing, taken from `wing_get` on a freshly set instance of that model. Whether a neutral stage loads switched off or in circuit and transparent is question 3 above; either is legal, a missing parameter is not. The same holds for any node that carries a `mdl` key, gates and effects slots included.

Whether a role relationship lives as data in the instrument entry or as logic in the render step, and whether the audit reads a rules file or carries its rules as assertions, are the reader's design calls; the guide only asks that each rule be stated somewhere a human can read it.

## The declaration it reads

The shape that has held up is three parts. An instrument library keyed by instrument, holding everything true of that instrument on your desk: filter, EQ, dynamics, sends, colour. A band file that lists the lineup as role, player, instrument key, in the channel order you want, plus the band's own buses and mute groups. And a render step that walks the lineup, assigns channel numbers in order, looks each instrument up in the library, and writes the nodes. A relationship between roles is a rule in the library that names another role, and the render step resolves it to a channel number for this date. Adding a band is one new band file; changing how an instrument is treated is one library entry.

## The file it renders

Full rules are in `wing-file-format.md`. In short:

- UTF-8 text, one node per line.
- Address, then a TAB, then `key=value` pairs separated by TABs. Emit tabs; the comma form is read for old files only.
- Blank lines and lines starting with `#` are comments and carry no address.
- A line with no TAB, or an address not starting with `/`, is a parse error.
- Values with spaces or commas are fine in the file; the pusher writes them to their own leaf address.

A push writes the desk's live state and nothing else, one node at a time in file order, each write followed by a read-back, so the desk changes as the push runs and a push is never done during a show. Line order carries no dependencies: a strip's `tags` can name a mute group before or after the `/mgrp` line that names the group. A line the parser cannot read is counted and skipped, an `incomplete` node is withheld, a key the profile excludes is skipped and named in the report, and the rest of the file is written; the report the push names carries the count of each. `wing_push_file` with `dry_run` runs every check and sends nothing, which is the lint pass to run before the first real push. There is no file size limit; a full scene of about 3,000 nodes is the normal case and takes several minutes. Saving the result as a recallable scene happens on the console surface, because the WING exposes no library save over OSC.

## The audit script

Write a second script that reads the same declaration the generator reads, then checks the rendered file against the rules you wrote down. Not against the generator's own logic, which only proves it agrees with itself, but against your rules as stated.

Have it report three numbers: nodes checked, nodes deliberately unchecked, and the reason each unchecked node is unchecked. The three should reconcile to the file's node count. That reconciliation is the point. A checker that reports "all clear" without saying what it did not look at is telling you nothing, and the nodes it quietly skipped are exactly the ones that will be wrong.
