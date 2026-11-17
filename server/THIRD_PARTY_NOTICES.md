# Third-party notices

## libwing — model catalog

`server/model_catalog.json` is derived from `propmap.jsonl` in
[libwing](https://github.com/dannyfiresnake/libwing) by its authors,
distributed under the Apache License 2.0 (its Cargo manifest also declares
MIT). The full license text is in `licenses/libwing-LICENSE.txt`.

What was taken: the names, types, ranges and labels of the WING's
processing-model parameters (EQ, gate, dynamics, filter, FX), as mapped by
libwing's authors on firmware 3.0.5. What was changed: the data was reduced
to model parameters only and reshaped into this server's type notation by
`tools/build_model_catalog.py`. It is used as reference data; the connected
desk remains the authority for every write.
