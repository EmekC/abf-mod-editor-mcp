"""Job entry for hidden Unreal processes. abf_job.py calls main(); the job comes from the JSON file named by ABF_MCP_JOB.

Commandlet ops (apply, compile, export) return normally so the log is flushed. Game and editor ops are generators
driven by a Slate tick: `yield <seconds>` sleeps, `yield ("file", path[, timeout])` waits for a file. The result is
written atomically to <run>/result.json as {"ok", "result", "error"}.
"""
import json
import os
import sys
import time
import traceback

import unreal

from . import core

_KEEP = []          # keeps the tick callback alive
FINISH_DELAY = 2.0  # seconds between the result file and the exit, so the log tail is flushed


def write_result(run, ok, result, error):
    tmp = os.path.join(run, "result.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "result": result, "error": error}, f, default=str)
    os.replace(tmp, os.path.join(run, "result.json"))


def _blueprint_like(path):
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    name = path.rsplit("/", 1)[-1]
    ad = reg.get_asset_by_object_path("%s.%s" % (path, name))
    if not ad.is_valid():
        return None
    return str(ad.asset_class_path.asset_name)


def op_apply(job):
    from . import apply
    reports, bps = [], []
    for i, spec in enumerate(job["assets"]):
        rep, bp = apply.apply_asset(spec, job["mod"], i)
        reports.append(rep)
        bps.append(bp)
    saved = 0
    for bp in bps:             # reached only when every asset applied without an exception
        if not unreal.EditorAssetLibrary.save_loaded_asset(bp, False):
            raise RuntimeError("could not save " + core.pth(bp))
        saved += 1
    return {"assets": reports, "saved": saved}


def op_compile(job):
    mod = job["mod"]
    paths = list(job.get("asset_paths") or [])
    if not paths:
        reg = unreal.AssetRegistryHelpers.get_asset_registry()
        for a in unreal.EditorAssetLibrary.list_assets("/" + mod, recursive=True, include_folder=False):
            p = str(a).split(".")[0]
            if "/_McpTest/" in p or "/_McpTmp/" in p:
                continue
            ad = reg.get_asset_by_object_path(str(a))
            if ad.is_valid() and str(ad.asset_class_path.asset_name) in ("Blueprint", "WidgetBlueprint", "AnimBlueprint"):
                paths.append(p)
    done, skipped, node_files = [], [], {}
    for p in paths:
        cls = _blueprint_like(p)
        if cls not in ("Blueprint", "WidgetBlueprint", "AnimBlueprint"):
            # class None = the registry does not know the asset: the caller must treat that as unchecked, not as clean
            skipped.append({"asset": p, "class": cls, "reason": "no registry entry" if cls is None else "not a Blueprint"})
            continue
        unreal.log("ABFMCP_COMPILE %s" % p)
        bp = unreal.load_asset(p)
        if bp is None:
            skipped.append({"asset": p, "class": cls, "reason": "load returned None"})
            continue
        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        done.append(p)
        if job.get("node_refs"):
            # the compiler keeps each message on its node (ErrorMsg=): the host reads the node names from this export
            fn = "compile_%d.t3d" % (len(done) - 1)
            try:
                core.export_text(bp, fn)
                node_files[p] = fn
            except Exception as e:
                unreal.log_warning("ABFMCP_COMPILE node export failed for %s: %s" % (p, e))
    return {"compiled": done, "skipped": skipped, "node_files": node_files}


def op_export(job):
    path = job["asset_path"]
    if path.startswith("/Game"):
        raise RuntimeError("never T3D-export a /Game Blueprint (crash)")
    cls = _blueprint_like(path)
    if cls not in ("Blueprint", "WidgetBlueprint", "AnimBlueprint"):
        raise RuntimeError("%s is not a Blueprint (%s)" % (path, cls))
    bp = core.must(unreal.load_asset(path), "asset " + path)
    unreal.BlueprintEditorLibrary.compile_blueprint(bp)
    text = core.export_text(bp, "export.t3d")
    res = {"file": "export.t3d", "chars": len(text)}
    write_result(job["run"], True, res, None)   # a hard crash below still leaves a usable export, reported as unavailable
    from . import readback
    res["live"] = readback.read_all(bp, text)
    return res


def _drive(gen, run):
    state = {"wake": 0.0, "file": None, "deadline": None, "done": False, "exit_at": None}

    def finish(ok, result, error):
        state["done"] = True
        write_result(run, ok, result, error)
        state["exit_at"] = time.time() + FINISH_DELAY

    def tick(_dt):
        now = time.time()
        if state["done"]:
            if now >= state["exit_at"]:
                os._exit(0)
            return
        try:
            if state["file"]:
                if os.path.exists(state["file"]):
                    state["file"] = None
                elif state["deadline"] and now > state["deadline"]:
                    p = state["file"]
                    state["file"] = None
                    gen.throw(RuntimeError("timed out waiting for " + p))
                else:
                    return
            if now < state["wake"]:
                return
            y = next(gen)
            if isinstance(y, (int, float)):
                state["wake"] = time.time() + y
            elif isinstance(y, tuple) and y and y[0] == "file":
                state["file"] = y[1]
                state["deadline"] = (time.time() + y[2]) if len(y) > 2 else None
        except StopIteration as e:
            finish(True, e.value, None)
        except Exception:
            finish(False, None, traceback.format_exc())

    _KEEP.append(unreal.register_slate_post_tick_callback(tick))


def _gen_op(name):
    if name == "game_test":
        from . import gametest
        return gametest.game_test
    from . import hiddenjob
    return getattr(hiddenjob, name)


def op_reflect(job):
    from . import reflect
    return reflect.run(job)


COMMANDLET_OPS = {"apply": op_apply, "compile": op_compile, "export": op_export, "reflect": op_reflect}
GENERATOR_OPS = ("game_test", "shots", "audit", "live_publish_test")


def main():
    job_path = os.environ["ABF_MCP_JOB"]
    run = os.path.dirname(job_path)
    try:
        # the startup hook turns remote execution on; a hidden process must not answer the server's discovery pings
        settings = unreal.find_object(None, "/Script/PythonScriptPlugin.Default__PythonScriptPluginSettings")
        if settings is not None:
            settings.set_editor_property("bRemoteExecution", False)
    except Exception:
        pass
    job = None
    try:
        with open(job_path, encoding="utf-8-sig") as f:
            job = json.load(f)
        core.configure("/%s/_McpTmp" % job["mod"], run)
        op = job["op"]
        if op in COMMANDLET_OPS:
            write_result(run, True, COMMANDLET_OPS[op](job), None)
        elif op in GENERATOR_OPS:
            _drive(_gen_op(op)(job), run)
        else:
            raise RuntimeError("unknown op %r" % op)
    except Exception:
        write_result(run, False, None, traceback.format_exc())
        if job and job.get("op") in GENERATOR_OPS:
            os._exit(0)
