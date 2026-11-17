#!/usr/bin/env python3
"""The model catalog: every parameter of every EQ, gate, dynamics, filter
and FX model, from libwing's propmap (firmware 3.0.5; see
licenses/libwing-LICENSE.txt and tools/build_model_catalog.py).

A profile walk only sees the model loaded on each slot when it ran, so on
its own the server can't list the E88 EQ's controls until E88 is loaded
somewhere, and a dry run can't predict a model change. This fills both
gaps. It is reference data: a write is still validated against the live
desk, and where the two disagree the desk wins.
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "model_catalog.json")

_cat = None


def catalog():
    global _cat
    if _cat is None:
        try:
            with open(PATH, encoding="utf-8") as f:
                _cat = json.load(f)
        except (OSError, ValueError):
            _cat = {"sections": {}}
    return _cat


def section_for(node):
    """'/ch/7/dyn' -> 'ch/dyn'; '/fx/3' -> 'fx'. Aux strips fall back to the
    channel sections the catalog doesn't list separately. None if the node
    isn't a model-bearing section."""
    secs = catalog()["sections"]
    m = re.match(r"^/(ch|aux|bus|main|mtx)/\d+/([a-z]+)$", node)
    if m:
        for key in ("%s/%s" % m.groups(), "ch/%s" % m.group(2)):
            if key in secs:
                return key
        return None
    if re.match(r"^/fx/\d+$", node):
        return "fx" if "fx" in secs else None
    return None


def model_params(node, model):
    """{param: {"type": raw, "label": ...}} for `model` on `node`'s section,
    or None if the catalog doesn't know it."""
    key = section_for(node)
    if not key:
        return None
    m = catalog()["sections"][key]["models"].get(str(model))
    return m["params"] if m else None


def models(section):
    s = catalog()["sections"].get(section)
    if not s:
        return None
    return {k: v["label"] for k, v in s["models"].items()}


def predict_shape(node, live_params, old_model, new_model):
    """The parameter set `node` should expose after switching from
    old_model to new_model: the live parameters that aren't specific to the
    old model, plus the new model's own. Returns None when the catalog
    doesn't know new_model. Shaped like Desk.describe()'s params."""
    new = model_params(node, new_model)
    if new is None:
        return None
    old = model_params(node, old_model) or {}
    shape = {k: v for k, v in live_params.items() if k not in old}
    for k, v in new.items():
        shape[k] = {"value": None, "readonly": False, "type": v["type"]}
    shape["mdl"] = dict(live_params.get("mdl", {}), value=new_model)
    return shape


def find(words, limit=60):
    """Catalog entries whose section, model, parameter or label contain all
    the words."""
    words = [w.lower() for w in words if w]
    hits = []
    for key, sec in sorted(catalog()["sections"].items()):
        base = "/%s/N/%s" % tuple(key.split("/")) if "/" in key else "/fx/N"
        for model, m in sorted(sec["models"].items()):
            for p, spec in sorted(m["params"].items()):
                hay = ("%s/%s %s %s %s" % (base, p, model, m["label"], spec.get("label", ""))).lower()
                if all(w in hay for w in words):
                    hits.append({"address": "%s/%s" % (base, p), "model": model,
                                 "model_label": m["label"], "label": spec.get("label"),
                                 "type": spec["type"], "source": "catalog (fw 3.0.5)"})
    return hits[:limit], len(hits)
