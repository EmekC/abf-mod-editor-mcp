"""Opt-in tools for abfc (https://github.com/EmekC/abf-mod-editor-mcp, see ABFC.md). Registered by server.py only when
ABF_ABFC_TOOLS=1. build() returns tuples in server.TOOLS form: (name, fn, description, properties, required).

Three tools: abfc_extensions (host only, no editor), reflect (hidden commandlet on the staging copy, read only) and
warm_session (start, stop or inspect the long-lived hidden commandlet that serves commandlet jobs when ABF_WARM_SESSION=1).
backend_info is not defined here; abfc reads it from the backend when the backend has it.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE / "editor") not in sys.path:
    sys.path.insert(0, str(HERE / "editor"))

import bpscan       # noqa: E402
import headless     # noqa: E402
import staging      # noqa: E402

EXT_PROTOCOL = 1
FEATURES = ["reflect", "compile_node_refs", "bp_read_seconds"]

STR, NUM = ({"type": "string"}, {"type": "number"})
STR_LIST = {"type": "array", "items": STR}

_PATH = re.compile(r"^/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*(\.[A-Za-z0-9_]+)?$")


def _out(d):
    return json.dumps(d, indent=2, default=str), bool(isinstance(d, dict) and d.get("error"))


def check_reflect_paths(mod, kind, paths):
    """Host-side refusal: only /Script/..., /Game/... and /<mod>/... object paths of plain characters. None when fine."""
    if not isinstance(paths, list):
        return "%s must be a list of strings" % kind
    for p in paths:
        if not isinstance(p, str) or not _PATH.match(p):
            return "%s: %r is not a plain object path" % (kind, p)
        if not (p.startswith("/Script/") or p.startswith("/Game/") or p.startswith("/%s/" % mod)):
            return "%s: %s must start with /Script/, /Game/ or /%s/" % (kind, p, mod)
    return None


def build(server_version):
    def t_abfc_extensions(args):
        return _out({"extProtocol": EXT_PROTOCOL, "serverVersion": server_version,
                     "python": sys.version.split()[0], "features": list(FEATURES)})

    def t_reflect(args):
        mod = args["mod"]
        classes = list(args.get("classes") or [])
        structs = list(args.get("structs") or [])
        casts = list(args.get("cast_targets") or [])
        for kind, paths_ in (("classes", classes), ("structs", structs), ("cast_targets", casts)):
            bad = check_reflect_paths(mod, kind, paths_)
            if bad:
                return _out({"error": bad})
        if not (classes or structs or casts):
            return _out({"error": "nothing to reflect (classes, structs and cast_targets are all empty)"})
        stage = staging.load(mod)
        try:
            res = headless.run_commandlet(stage, {"op": "reflect", "classes": classes, "structs": structs, "cast_targets": casts},
                                          timeout=float(args.get("timeout_seconds") or 900))
        finally:
            staging.strip_scratch(stage)
        if not res["ok"]:
            return _out({"error": res.get("error") or "reflect run failed", "run": res["run"], "log_tail": res.get("log_tail")})
        rules, meta = bpscan.load_filter()
        counts = (res.get("result") or {}).get("counts") or {}
        return _out({"ok": True, "run": res["run"], "seconds": res["seconds"], "file": str(Path(res["run"]) / "reflect.json"),
                     "counts": counts,
                     "filter": {"content_version": meta.get("content_version"), "rules": [[v, k] for k, v in sorted(rules.items())]}})

    def t_warm_session(args):
        action = args.get("action")
        if action not in ("start", "stop", "status"):
            return _out({"error": "action must be start, stop or status"})
        if not headless.warm_enabled():
            return _out({"error": "warm sessions are off: start the server with ABF_WARM_SESSION=1", "running": False})
        mod = args.get("mod")
        try:
            if action == "start":
                if not mod:
                    return _out({"error": "start needs mod (the staged mod to keep a warm editor for)"})
                return _out(headless.WARMS.get(staging.load(mod)).start())
            if action == "stop":
                stopped = headless.WARMS.stop_all()
                return _out(dict(headless.WARMS.status(mod), stopped=stopped))
            return _out(headless.WARMS.status(mod))
        except headless.HeadlessError as e:
            return _out({"error": str(e), "running": False})

    return [
        ("abfc_extensions", t_abfc_extensions,
         "Read-only, needs no editor: abfc extension features of this server (enabled with ABF_ABFC_TOOLS=1).",
         {}, []),
        ("reflect", t_reflect,
         "Read-only, hidden editor on the staging copy: enumerate functions, properties, struct fields, ancestors and cast pins of classes "
         "(/Script, /Game, mod) into <run>/reflect.json, and return the Blueprint filter rules. Never loads /Game assets as assets.",
         {"mod": STR, "classes": STR_LIST, "structs": STR_LIST, "cast_targets": STR_LIST, "timeout_seconds": NUM}, ["mod", "classes"]),
        ("warm_session", t_warm_session,
         "Start, stop or inspect the warm session: one hidden commandlet kept alive on the staging copy that serves bp_apply, bp_read, "
         "bp_compile_report and reflect without a new editor start per call (needs ABF_WARM_SESSION=1). Returns {running, pid, jobs, startedAt}. "
         "bp_game_test, mod_audit and hidden_editor_shot always run cold and stop a running session first (they share its -userdir).",
         {"action": {"type": "string", "enum": ["start", "stop", "status"]}, "mod": STR}, ["action"]),
    ]
