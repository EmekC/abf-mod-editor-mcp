"""Generator ops that run in an editor with UI on a hidden desktop (see hiddendesk.py on the host side)."""
import json
import os
import re
import shutil
import time

import unreal

from . import cursorpatch, livepublish


def _touch(path):
    open(path, "w").close()


def boot(run, mod, timeout=300):
    """Patch the cursor, apply cursor.json when the host writes it (the host clicks the picker tile), and wait until
    the mod is mounted."""
    data = cursorpatch.install()
    last = None
    t0 = time.time()
    while not unreal.PluginBlueprintLibrary.is_plugin_mounted(mod):
        cur = os.path.join(run, "cursor.json")
        if os.path.exists(cur):
            try:
                with open(cur, encoding="utf-8") as f:
                    v = json.load(f)
            except (OSError, ValueError):
                v = None
            if v and v != last:
                last = v
                cursorpatch.setpos(data, int(v["x"]), int(v["y"]))
                _touch(os.path.join(run, "cursor_ok"))
        if time.time() - t0 > timeout:
            raise RuntimeError("mod %s was not mounted within %d s (the Mod Browser tile may not have been clicked)" % (mod, timeout))
        yield 0.2
    _touch(os.path.join(run, "mounted"))
    yield 10.0


def shots(job):
    run = job["run"]
    yield from boot(run, job["mod"])
    sub = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    out = []
    for i, path in enumerate(job.get("assets") or []):
        a = unreal.load_asset(path)
        if a is None:
            out.append({"asset": path, "ok": False, "error": "asset not found"})
            continue
        sub.open_editor_for_assets([a])
        yield float(job.get("settle_seconds", 9))
        tag = str(i)
        _touch(os.path.join(run, "shotreq_" + tag))
        yield ("file", os.path.join(run, "shotdone_" + tag), 90)
        sub.close_all_editors_for_asset(a)
        a = None
        out.append({"asset": path, "ok": True, "tag": tag})
        yield 1.0
    return out


def audit(job):
    run = job["run"]
    yield from boot(run, job["mod"])
    w = unreal.new_object(unreal.AbioticModCreateUploadWidget)
    returned = w.audit_assets()
    t0 = time.time()
    done = False
    while time.time() - t0 < 30:
        try:
            with open(job["log"], encoding="utf-8", errors="replace") as f:
                if "Asset audit complete" in f.read():
                    done = True
                    break
        except OSError:
            pass
        yield 1.0
    return {"returned": returned, "completed_in_log": done}


def _vars_of(asset, run):
    text = ""
    e = unreal.AssetExportTask()
    fn = os.path.join(run, "lp_export.t3d").replace("\\", "/")
    e.object = asset
    e.filename = fn
    e.exporter = unreal.ObjectExporterT3D()
    e.automated = True
    e.replace_identical = True
    e.prompt = False
    if unreal.Exporter.run_asset_export_task(e):
        with open(fn, encoding="utf-8-sig") as f:
            text = f.read()
    return re.findall(r'^   NewVariables\(\d+\)=\(VarName="([^"]+)"', text, re.M)


def live_publish_test(job):
    """The ed12 sequence through livepublish.py: a modified asset is refused, a clean one is unloaded, its file is
    replaced, and it is reloaded and read back."""
    run = job["run"]
    yield from boot(run, job["mod"])
    clean, dirty = job["clean_asset"], job["dirty_asset"]
    sub = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)
    out = {}
    c = unreal.load_asset(clean)
    d = unreal.load_asset(dirty)
    if c is None or d is None:
        raise RuntimeError("test assets not found: %s %s" % (clean, dirty))
    out["vars_before"] = _vars_of(c, run)
    sub.open_editor_for_assets([c])
    yield 3.0
    d.modify(True)
    out["refused_dirty"] = livepublish.prepare({"packages": [clean, dirty]})
    c = None
    out["prepared"] = livepublish.prepare({"packages": [clean]})
    yield 1.0
    shutil.copyfile(job["replacement"], job["target"])
    yield 1.0
    out["reload"] = livepublish.reload_assets({"root": "/" + job["mod"], "packages": [clean]})
    c2 = unreal.load_asset(clean)
    out["vars_after"] = _vars_of(c2, run) if c2 is not None else None
    return out
