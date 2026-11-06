"""Warm session loop: one long-lived hidden `-run=pythonscript` commandlet that serves many commandlet jobs.

The host (headless.WarmSession) writes a job file like a cold run does (<run>/job.json) and drops a small queue entry
<warm dir>/queue/<seq>-<id>.json = {"id", "job", "run"}. This loop takes entries in name order. Before every job it
rescans the mod folder and reloads the mod's loaded packages from disk (the host may have rolled files back, or
staging_sync may have replaced them since the last job), then runs the job through the same dispatcher a cold process
uses (jobs.COMMANDLET_OPS), writes <run>/result.json and finally <run>/warm.done. The queue entry is deleted last.

The log is shared by all jobs of the session; the loop brackets each job with ABFMCP_WARM_BEGIN/END <id> lines so the host
can cut the job's part out of the log.

Exits when <warm dir>/stop.json exists, or after ABF_WARM_IDLE_SECONDS (default 600) without a job.
"""
import json
import os
import time
import traceback

import unreal

from . import core, jobs

POLL_SECONDS = 0.2
DEFAULT_IDLE_SECONDS = 600.0


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _package_file(stage_dir, mod, package):
    """/<Mod>/A/B -> <stage>/Content/A/B.uasset (or None when the package is not under the mod)."""
    prefix = "/%s/" % mod
    if not stage_dir or not package.startswith(prefix):
        return None
    return os.path.join(stage_dir, "Content", package[len(prefix):].replace("/", os.sep) + ".uasset")


RELOAD_FAILED = "ABFMCP_WARM_RELOAD_FAILED"      # prefix of the result error the host turns into a cold fallback


class ReloadFailed(Exception):
    """The reload/unload of the mod's packages did not succeed: compiling now would use stale in-memory packages."""


def call_problem(what, res):
    """None when the result of reload_packages/unload_packages shows no failure, else a short text. The Python binding may
    return a bool, or a tuple of (bools, error text) for the out parameters; a non-empty error text or a plain False is a
    failure. A tuple of bools alone is not judged (the meaning of its flags is not documented): it is logged in the report."""
    items = list(res) if isinstance(res, (tuple, list)) else [res]
    for it in items:
        if it is None or isinstance(it, (bool, int, float)):
            continue
        text = str(it).strip()
        if text:
            return "%s: %s" % (what, text[:300])
    if not isinstance(res, (tuple, list)) and res is False:
        return "%s returned False" % what
    return None


def reload_mod(mod, stage_dir):
    """Rescan /<Mod> and reload every loaded package under it from disk; unload loaded packages whose file is gone.
    Scratch packages (/<Mod>/_McpTmp, never saved) are left alone. Returns a small report (also logged)."""
    root = "/" + mod
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    reg.scan_paths_synchronous([root], True)
    names = []
    for a in unreal.EditorAssetLibrary.list_assets(root, recursive=True, include_folder=False):
        p = str(a).split(".")[0]
        if "/_McpTmp/" in p or p in names:
            continue
        names.append(p)
    reload_pkgs, gone_pkgs = [], []
    for name in names:
        pkg = unreal.find_package(name)
        if pkg is None:
            continue
        f = _package_file(stage_dir, mod, name)
        if f is not None and not os.path.isfile(f):
            gone_pkgs.append(pkg)
        else:
            reload_pkgs.append(pkg)
    report = {"reloaded": len(reload_pkgs), "unloaded": len(gone_pkgs)}
    problems = []
    if gone_pkgs:
        res = unreal.EditorLoadingAndSavingUtils.unload_packages(gone_pkgs)
        report["unload"] = str(res)[:200]
        problems.append(call_problem("unload_packages", res))
    if reload_pkgs:
        res = unreal.EditorLoadingAndSavingUtils.reload_packages(reload_pkgs, unreal.ReloadPackagesInteractionMode.ASSUME_POSITIVE)
        report["reload"] = str(res)[:200]
        problems.append(call_problem("reload_packages", res))
    problems = [p for p in problems if p]
    if problems:
        report["problems"] = problems
    return report


def run_entry(entry, stage_dir):
    run = entry["run"]
    jid = entry["id"]
    unreal.log("ABFMCP_WARM_BEGIN %s" % jid)
    try:
        with open(entry["job"], encoding="utf-8-sig") as f:
            job = json.load(f)
        op = job["op"]
        if op not in jobs.COMMANDLET_OPS:
            raise RuntimeError("op %r is not a commandlet op: it cannot run in a warm session" % (op,))
        t0 = time.time()
        try:
            report = reload_mod(job["mod"], stage_dir)
        except Exception:
            raise ReloadFailed("the reload raised:\n" + traceback.format_exc())
        unreal.log("ABFMCP_WARM_RELOAD %s seconds=%.1f" % (json.dumps(report), time.time() - t0))
        if report.get("problems"):
            # the job would compile the stale in-memory packages and could report clean: fail before it runs, so the host
            # stops the session and runs the job cold
            raise ReloadFailed("; ".join(report["problems"]))
        core.configure("/%s/_McpTmp" % job["mod"], run)
        jobs.write_result(run, True, jobs.COMMANDLET_OPS[op](job), None)
    except ReloadFailed as e:
        jobs.write_result(run, False, None, "%s: %s" % (RELOAD_FAILED, e))
    except Exception:
        jobs.write_result(run, False, None, traceback.format_exc())
    unreal.log("ABFMCP_WARM_END %s" % jid)
    _write_json(os.path.join(run, "warm.done"), {"id": jid, "time": time.time()})


def main():
    try:
        settings = unreal.find_object(None, "/Script/PythonScriptPlugin.Default__PythonScriptPluginSettings")
        if settings is not None:
            settings.set_editor_property("bRemoteExecution", False)
    except Exception:
        pass
    wd = os.environ["ABF_MCP_WARM_DIR"]
    stage_dir = os.environ.get("ABF_MCP_STAGE_DIR")
    try:
        idle = float(os.environ.get("ABF_WARM_IDLE_SECONDS") or DEFAULT_IDLE_SECONDS)
    except ValueError:
        idle = DEFAULT_IDLE_SECONDS
    queue = os.path.join(wd, "queue")
    _write_json(os.path.join(wd, "ready.json"), {"pid": os.getpid(), "time": time.time()})
    unreal.log("ABFMCP_WARM_READY pid=%d" % os.getpid())
    last = time.time()
    while True:
        if os.path.exists(os.path.join(wd, "stop.json")):
            unreal.log("ABFMCP_WARM_EXIT stop requested")
            break
        entries = sorted(n for n in os.listdir(queue) if n.endswith(".json"))
        if entries:
            path = os.path.join(queue, entries[0])
            try:
                with open(path, encoding="utf-8-sig") as f:
                    entry = json.load(f)
                run_entry(entry, stage_dir)
            except Exception:
                unreal.log_error("ABFMCP_WARM_ENTRY_FAILED %s" % traceback.format_exc())
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            last = time.time()
            continue
        if time.time() - last > idle:
            unreal.log("ABFMCP_WARM_EXIT idle for %.0f s" % idle)
            break
        time.sleep(POLL_SECONDS)
