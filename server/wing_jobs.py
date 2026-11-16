#!/usr/bin/env python3
"""Background jobs for the long read-only walks: wing_dump and
wing_generate_profile.

A full /ch dump is about 1,800 nodes and a whole-desk profile walk about
6,000. Either can outlast a single MCP tool call, which is why wing_push_file
became a background job in v0.3.0; v0.4.0 gives the walks the same shape.
Each tool starts a WalkJob and waits briefly (INLINE_WAIT_S). A small walk
finishes inside that wait and its result comes straight back; a large one
returns a job id to poll with wing_job_status.

Both walks go through wing_harvest.walk_tree, so an overflowing node is
re-read names-only and its subtree is still walked, and anything lost is
named in the output rather than silently missing.
"""

import json
import os
import threading
import time

import wing_harvest as wh
import wing_profile as wp

INLINE_WAIT_S = 20.0


class WalkJob:
    kind = "walk"

    def __init__(self, job_id, desk):
        self.job_id = job_id
        self.desk = desk
        self.state = "running"          # running | done | failed
        self.nodes_done = 0
        self.queued = 0
        self.started = time.time()
        self.finished = None
        self.error = None
        self.result = None
        self._lock = threading.Lock()
        self._thread = None

    # --- shared plumbing, same contract as wing_push_core.PushJob
    def is_running(self):
        with self._lock:
            return self.state == "running"

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="wing-%s-%s" % (self.kind, self.job_id))
        self._thread.start()

    def wait(self, seconds):
        self._thread.join(seconds)
        return not self.is_running()

    def _progress(self, done, queued):
        with self._lock:
            self.nodes_done, self.queued = done, queued

    def _run(self):
        try:
            res = self.work()
            with self._lock:
                self.result, self.state, self.finished = res, "done", time.time()
        except Exception as exc:
            with self._lock:
                self.state, self.error = "failed", "%s: %s" % (type(exc).__name__, exc)
                self.finished = time.time()

    def snapshot(self):
        with self._lock:
            out = {"job_id": self.job_id, "kind": self.kind, "state": self.state,
                   "nodes_done": self.nodes_done, "queued": self.queued,
                   "elapsed_seconds": round((self.finished or time.time())
                                            - self.started, 1),
                   "error": self.error}
            if self.result is not None:
                out["result"] = self.result
            return out


def default_dump_path(root):
    stem = (root.strip("/").replace("/", "-").replace("$", "") or "root")
    return os.path.join(wp.USER_DIR, "dumps",
                        "%s_%s.wing" % (stem, time.strftime("%Y%m%d-%H%M%S")))


def resolve_out_path(path, root):
    """Absolute path for a dump. A relative path is taken under the user
    dir's dumps/ folder: Claude Desktop starts the server in whatever
    directory it likes, so 'relative to the cwd' means nothing useful."""
    if not path:
        return default_dump_path(root)
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(wp.USER_DIR, "dumps", path)
    return path


class DumpJob(WalkJob):
    kind = "dump"

    def __init__(self, job_id, desk, root, out_path, max_nodes, ident):
        super().__init__(job_id, desk)
        self.root, self.out_path, self.max_nodes = root, out_path, max_nodes
        self.ident = ident or {}

    def work(self):
        walked, stats = wh.walk_tree(self.desk, self.root, self.max_nodes,
                                     self._progress)
        lines = wh.show_lines(walked)
        header = [
            "# dump of %s from %s (%s)" % (self.root, self.ident.get("name", "?"),
                                          self.ident.get("model", "?")),
            "# %s   firmware %s" % (time.strftime("%Y-%m-%d %H:%M"),
                                    self.ident.get("firmware", "?")),
            "# %d nodes read, %d lines. Pairs are TAB separated." % (
                stats["nodes"], len(lines)),
        ] + wh.loss_header(stats) + [""]
        os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
        with open(self.out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(header + lines) + "\n")
        return {"root": self.root, "path": self.out_path,
                "nodes_read": stats["nodes"], "lines": len(lines),
                "values_not_captured": stats["no_values"],
                "subtree_lost": stats["overflow"], "no_reply": stats["no_reply"],
                "node_cap_hit_unread": stats["unread"],
                "preview": lines[:15]}


class ProfileJob(WalkJob):
    kind = "profile"

    def __init__(self, job_id, desk, ident, max_nodes, out_path=None):
        super().__init__(job_id, desk)
        self.ident, self.max_nodes, self.out_path = ident or {}, max_nodes, out_path

    def work(self):
        walked, stats = wh.walk_tree(self.desk, "/", self.max_nodes, self._progress)
        nodes = {n: {"children": g["children"], "params": g["params"]}
                 for n, g, _ in walked}
        raw = {"identity": self.ident,
               "stats": {"elapsed_s": stats["seconds"], "node_count": len(nodes),
                         "overflow_nodes": stats["overflow"],
                         "names_only_nodes": stats["no_values"],
                         "no_reply_nodes": stats["no_reply"],
                         "unread": stats["unread"]},
               "nodes": nodes}
        slug = wp.model_slug(self.ident.get("model"))
        map_path = os.path.join(wp.USER_DIR, "maps", "%s_%s.json" % (
            slug, time.strftime("%Y%m%d-%H%M%S")))
        os.makedirs(os.path.dirname(map_path), exist_ok=True)
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=1, sort_keys=True)
        prof, prof_path = wp.build(map_path, self.out_path)
        return {"model": self.ident.get("model"), "map": map_path,
                "profile": prof_path, "firmware": prof["firmware"],
                "nodes": len(nodes), "counts": prof["counts"],
                "overflow_nodes": stats["overflow"],
                "names_only_nodes": stats["no_values"],
                "no_reply_nodes": stats["no_reply"],
                "node_cap_hit_unread": stats["unread"]}
