"""The nine headless Blueprint tools. build() returns tuples in server.TOOLS form: (name, fn, description, properties, required).

Everything runs in hidden processes on a staging copy of the mod; only bp_publish writes into the real mod, and it can
reach the user's editor (through the injected run_json) to unload and reload assets when the mod is open there.
No mouse, keyboard, focus or clipboard is used anywhere in this module.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE / "editor") not in sys.path:
    sys.path.insert(0, str(HERE / "editor"))

import bpscan       # noqa: E402
import headless     # noqa: E402
import hiddendesk   # noqa: E402
import paths        # noqa: E402
import staging      # noqa: E402
from bpkit import specs   # noqa: E402
LIVEPUBLISH = HERE / "editor" / "bpkit" / "livepublish.py"
HARNESS_ASSET = "_McpTest/BP_McpHarness"

STR, NUM, INT, BOOL, OBJ = ({"type": "string"}, {"type": "number"}, {"type": "integer"}, {"type": "boolean"}, {"type": "object"})
ARR = {"type": "array"}


def harness_spec(mod):
    """Blueprint with SpawnAny / MakeAny: Python cannot spawn actors or create widgets in -game, a Blueprint can."""
    return {
        "asset": "/%s/%s" % (mod, HARNESS_ASSET),
        "create": {"class": "Blueprint", "parent": "/Script/Engine.Actor", "if_missing": True},
        "functions": [
            {"name": "SpawnAny", "inputs": [{"name": "C", "type": "class:/Script/Engine.Actor"}],
             "outputs": [{"name": "A", "type": "object:/Script/Engine.Actor"}],
             "nodes": [{"id": "mt", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "MakeTransform"},
                       {"id": "sp", "k": "spawn"}],
             "wires": [["entry.then", "sp.execute"], ["sp.then", "result.execute"], ["mt.ReturnValue", "sp.SpawnTransform"],
                       ["entry.C", "sp.Class"], ["sp.ReturnValue", "result.A"]]},
            {"name": "MakeAny", "inputs": [{"name": "C", "type": "class:/Script/UMG.UserWidget"}],
             "outputs": [{"name": "W", "type": "object:/Script/UMG.UserWidget"}],
             "nodes": [{"id": "cw", "k": "create_widget"}],
             "wires": [["entry.then", "cw.execute"], ["cw.then", "result.execute"], ["entry.C", "cw.Class"], ["cw.ReturnValue", "result.W"]]},
        ],
    }


def filter_editor_processes(rows, exe, headless_root):
    """Rows are {"pid","path","command_line"}. Keep the editors that could hold the user's mod:
    drop another kit install (readable path that differs from `exe`) and this tool's own hidden processes (their command
    line carries our private -userdir); a row whose path or command line is unreadable (None) is kept, fail safe."""
    own = "-userdir=%s/ud" % str(headless_root).replace("\\", "/").lower().rstrip("/")
    want = os.path.normcase(os.path.abspath(str(exe))) if exe else None
    out = []
    for r in rows:
        path, cmd = r.get("path"), r.get("command_line")
        if path and want and os.path.normcase(os.path.abspath(path)) != want:
            continue
        if cmd and own in cmd.replace("\\", "/").lower():
            continue
        out.append(r)
    return out


def list_editor_processes():
    """Read-only process listing (no handle to the editor is opened, nothing is sent to it): PowerShell + CIM, no window."""
    name = paths.EDITOR_EXE_REL.name
    cmd = ('Get-CimInstance Win32_Process -Filter "Name=\'%s\'" | Select-Object ProcessId,ExecutablePath,CommandLine '
           '| ConvertTo-Json -Compress' % name)
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True,
                           errors="replace", timeout=20, creationflags=0x08000000)
    except (OSError, subprocess.SubprocessError) as e:
        raise headless.HeadlessError("could not list editor processes: %s" % e)
    if r.returncode != 0:
        raise headless.HeadlessError("listing editor processes failed (exit %d)" % r.returncode)
    text = (r.stdout or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        raise headless.HeadlessError("could not read the process list")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise headless.HeadlessError("could not read the process list")
    rows = [{"pid": x.get("ProcessId"), "path": x.get("ExecutablePath"), "command_line": x.get("CommandLine")} for x in data]
    kept = filter_editor_processes(rows, paths.editor_exe(), headless.root())
    # the command line carries a login code: it is used for filtering only and never leaves this function
    return [{"pid": r["pid"], "path": r["path"]} for r in kept]


def valid_map(mod, map_path):
    """A map argument becomes an exe argument ahead of -userdir: only a plain /Game or /<Mod> package path is allowed."""
    return isinstance(map_path, str) and re.match(r"^/(Game|%s)/[A-Za-z0-9_/]+$" % re.escape(mod), map_path) is not None


def unchecked(compile_result):
    """Assets a compile run could not check: unknown to the registry or not loadable. Legitimate non-Blueprint skips are not listed."""
    return [s for s in (compile_result or {}).get("skipped", [])
            if s.get("class") is None or s.get("reason") in ("load returned None", "no registry entry")]


class LiveEditor:
    """Talks to the user's own editor, only when the mod is open there. Sends livepublish.py through run_json."""

    def __init__(self, run_json, remote_errors, py_status, mod, source_dir=None, list_processes=None):
        self.run_json = run_json
        self.remote_errors = remote_errors
        self.py_status = py_status
        self.mod = mod
        self.source_dir = source_dir
        self.list_processes = list_processes or list_editor_processes
        self.detail = ""

    STATUS_TAIL = (
        "\n_status_inner = _abf\n"
        "def _abf(args):\n"
        "    import os\n"
        "    r = _status_inner(args)\n"
        "    r['pid'] = os.getpid()\n"
        "    r['mounted'] = bool(unreal.PluginBlueprintLibrary.is_plugin_mounted(args['mod']))\n"
        "    r['dir_exists'] = bool(unreal.EditorAssetLibrary.does_directory_exist('/' + args['mod']))\n"
        "    return r\n")

    def state(self, mod):
        """closed (no editor process: nothing is contacted), open, other, or unreachable.
        Exactly one editor process must exist and it must be the one that answers (its pid is in the reply): with two editors,
        or an answer from another process, we cannot know who holds the mod and its unsaved work."""
        self.detail = ""
        try:
            procs = self.list_processes()
        except Exception as e:
            self.detail = "Could not list editor processes (%s)." % e
            return "unreachable"
        if not procs:
            return "closed"
        pids = ", ".join(str(p.get("pid")) for p in procs)
        if len(procs) != 1:
            self.detail = "More than one editor process is running (%s), so it cannot be known which one holds the mod." % pids
            return "unreachable"
        try:
            st = self.run_json(self.py_status + self.STATUS_TAIL, {"mod": mod}, timeout=30)
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self.detail = "Editor process %s did not answer (%s: %s)." % (pids, type(e).__name__, e)
            return "unreachable"
        mods = st.get("open_mods") if isinstance(st, dict) else None
        if not isinstance(mods, list) or not isinstance(st.get("mounted"), bool) or not isinstance(st.get("dir_exists"), bool):
            self.detail = "Editor process %s answered without the expected status." % pids
            return "unreachable"
        try:
            same = int(st.get("pid")) == int(procs[0].get("pid"))
        except (TypeError, ValueError):
            same = False
        if not same:
            self.detail = "The answer to the status request did not come from the editor process %s." % pids
            return "unreachable"
        if st["mounted"] or st["dir_exists"]:
            return "open"
        for m in mods:
            if isinstance(m, dict) and str(m.get("root", "")).strip("/").lower() == mod.lower():
                return "open"
        return "other"

    def _call(self, fn, args):
        code = LIVEPUBLISH.read_text(encoding="utf-8") + "\ndef _abf(args):\n    return %s(args)\n" % fn
        return self.run_json(code, args, timeout=180)

    def prepare(self, packages):
        return self._call("prepare", {"packages": packages})

    def reload(self, packages):
        return self._call("reload_assets", {"root": "/" + self.mod, "packages": packages})


def _out(d):
    return json.dumps(d, indent=2, default=str), bool(isinstance(d, dict) and d.get("error"))


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _in_mod(path, mod):
    if not isinstance(path, str) or path.startswith("/Game"):
        return "/Game assets are never read or edited here"
    if not path.startswith("/%s/" % mod):
        return "asset path must start with /%s/" % mod
    return None


def build(run_json, remote_errors, t3d_errors, kit_root, forbidden, py_status, list_processes=list_editor_processes):
    def visible_compile(parsed):
        return {"errors": [e for e in parsed["errors"] if "/_McpTmp/" not in (e["asset"] or "")],
                "warnings": [w for w in parsed["warnings"] if "/_McpTmp/" not in (w["asset"] or "")]}

    # ---------------------------------------------------------------- staging
    def t_staging_sync(args):
        stage = staging.sync(args["mod_dir"], bool(args.get("discard_changes")), forbidden)
        return _out({"mod": stage.name, "files": stage.file_count, "staging_path": str(stage.dir),
                     "message": "Staged %s. bp_apply and the other tools work on this copy; bp_publish copies the result back." % stage.name})

    def t_staging_status(args):
        stage = staging.load(args["mod"])
        meta = stage.meta()
        d = staging.diff(stage)
        return _out({"mod": stage.name, "staging_path": str(stage.dir), "source_dir": meta.get("source_dir"),
                     "synced_at": meta.get("synced_at"), "changed": d["changed"], "added": d["added"], "removed": d["removed"],
                     "conflicts": d["conflicts"], "kit_found": paths.find_editor_root() is not None})

    # ---------------------------------------------------------------- apply
    def apply_batch(stage, batch, timeout, keep_on_error):
        mod = stage.name
        problems = specs.validate(batch, mod)
        if problems:
            return {"ok": False, "error": "invalid spec", "problems": problems}
        stems = ["Content/" + a["asset"][len("/%s/" % mod):] for a in batch["assets"]]
        existing = {}

        def snapshot(run):
            before = run / "before"
            for stem in stems:
                for rel in staging.files_for(stage, stem):
                    dst = before / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(stage.dir / rel, dst)
                    existing[rel] = dst

        res = headless.run_commandlet(stage, {"op": "apply", "assets": batch["assets"]}, timeout=timeout, prepare=snapshot)
        run = Path(res["run"])
        staging.strip_scratch(stage)
        parsed = headless.parse_compile_log(_read(res["log"]), mod)
        compile_ = visible_compile(parsed)
        targets = {a["asset"] for a in batch["assets"]}
        target_errors = [e for e in compile_["errors"] if e["asset"] in targets]
        t3d_problems = []
        for i, a in enumerate(batch["assets"]):
            f = run / ("final_%d.t3d" % i)
            if f.is_file():
                r = t3d_errors(_read(f))
                for p in r["problems"]:
                    t3d_problems.append(dict(p, asset=a["asset"]))
        ok = bool(res["ok"]) and not target_errors
        rolled_back = False
        if not ok and not keep_on_error:
            for stem in stems:
                for rel in staging.files_for(stage, stem):
                    if rel not in existing:
                        (stage.dir / rel).unlink()
            for rel, src in existing.items():
                dst = stage.dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            rolled_back = True
        out = {"ok": ok, "rolled_back": rolled_back, "assets": (res.get("result") or {}).get("assets"), "compile": compile_,
               "problems": t3d_problems, "run": res["run"], "seconds": res["seconds"]}
        if not ok:
            out["error"] = (res.get("error") or ("%d compile error(s) in the edited assets" % len(target_errors)))
            if res.get("log_tail"):
                out["log_tail"] = res["log_tail"]
            if rolled_back:
                out["note"] = "The staged files were restored to their state before this call."
            elif keep_on_error:
                out["note"] = "keep_on_error: the staged files were left as the failed run wrote them (or unchanged if it never saved)."
        return out

    def load_batch(args):
        if args.get("spec_file"):
            data = json.loads(Path(args["spec_file"]).read_text(encoding="utf-8-sig"))
            return {"assets": data} if isinstance(data, list) else data
        a = args.get("assets")
        if isinstance(a, dict) and "assets" in a:
            return a
        return {"assets": a}

    def t_bp_apply(args):
        stage = staging.load(args["mod"])
        return _out(apply_batch(stage, load_batch(args), float(args.get("timeout_seconds") or 600), bool(args.get("keep_on_error"))))

    # ---------------------------------------------------------------- read / compile
    def t_bp_read(args):
        mod, path = args["mod"], args["asset_path"]
        bad = _in_mod(path, mod)
        if bad:
            return _out({"error": bad})
        stage = staging.load(mod)
        res = headless.run_commandlet(stage, {"op": "export", "asset_path": path}, timeout=float(args.get("timeout_seconds") or 300))
        if not res["ok"]:
            return _out({"error": res.get("error") or "export failed", "run": res["run"], "log_tail": res.get("log_tail")})
        st = bpscan.parse_structure(_read(Path(res["run"]) / "export.t3d"))
        if not st["blueprint"]:
            return _out({"error": "the text export of %s has no Blueprint object" % path, "run": res["run"]})
        bpscan.merge_live(st, res["result"].get("live"))
        if args.get("graph"):
            st["graphs"] = {k: v for k, v in st["graphs"].items() if k == args["graph"]}
        if not args.get("include_pins"):
            for nodes in st["graphs"].values():
                for n in nodes:
                    n.pop("pins", None)
                    n.pop("object_refs", None)
        st["asset"] = path
        st["run"] = res["run"]
        st["seconds"] = res.get("seconds")
        return _out(st)

    def t_bp_compile_report(args):
        mod = args["mod"]
        stage = staging.load(mod)
        paths_ = list(args.get("asset_paths") or [])
        for p in paths_:
            bad = _in_mod(p, mod)
            if bad:
                return _out({"error": "%s: %s" % (p, bad)})
        res = headless.run_commandlet(stage, {"op": "compile", "asset_paths": paths_}, timeout=float(args.get("timeout_seconds") or 600))
        if not res["ok"]:
            return _out({"error": res.get("error") or "compile run failed", "run": res["run"], "log_tail": res.get("log_tail")})
        c = visible_compile(headless.parse_compile_log(_read(res["log"]), mod))
        per = {}
        for kind in ("errors", "warnings"):
            for e in c[kind]:
                per.setdefault(e["asset"], {"errors": [], "warnings": []})[kind].append(e["message"])
        bad = unchecked(res["result"])
        messages = [{"severity": sev, "asset": e["asset"], "message": e["message"], "node": e.get("node"), "graph": e.get("graph")}
                    for sev, kind in (("error", "errors"), ("warning", "warnings")) for e in c[kind]]
        return _out({"clean": not c["errors"] and not bad, "unchecked": bad, "error_count": len(c["errors"]), "warning_count": len(c["warnings"]),
                     "assets": per, "messages": messages, "compiled": res["result"]["compiled"], "skipped": res["result"]["skipped"],
                     "run": res["run"], "seconds": res["seconds"]})

    # ---------------------------------------------------------------- game test
    def t_bp_game_test(args):
        mod = args["mod"]
        stage = staging.load(mod)
        steps = args.get("steps") or []
        if not steps:
            return _out({"error": "steps is empty"})
        notes = []
        if any(s.get("op") in ("spawn", "widget") for s in steps):
            if not (stage.dir / "Content" / (HARNESS_ASSET + ".uasset")).is_file():
                r = apply_batch(stage, {"assets": [harness_spec(mod)]}, 600, False)
                if not r["ok"]:
                    return _out({"error": "could not build the test harness: %s" % r.get("error"), "harness": r})
                notes.append("built the test harness in %s (Content/_McpTest, never published)" % HARNESS_ASSET)
        map_path = args.get("map") or "/Game/Maps/Empty"
        if not valid_map(mod, map_path):
            return _out({"error": "map must look like /Game/Maps/Empty or /%s/Maps/Name (letters, digits, _ and /)" % mod})
        res = headless.run_game(stage, {"op": "game_test", "steps": steps, "map": map_path}, map_path=map_path,
                                timeout=float(args.get("timeout_seconds") or 180))
        run = Path(res["run"])
        result = res.get("result") or {}
        step_results = result.get("steps")
        if step_results is None:
            step_results = []
            for line in _read(run / "progress.jsonl").splitlines():
                try:
                    step_results.append(json.loads(line))
                except ValueError:
                    pass
        passed = sum(1 for s in step_results if s.get("ok"))
        failed = sum(1 for s in step_results if not s.get("ok"))
        complete = len(step_results) == len(steps)
        ok = bool(res["ok"]) and failed == 0 and complete
        out = {"ok": ok, "steps": step_results, "passed": passed, "failed": failed, "complete": complete,
               "runtime_log": headless.parse_runtime_log(_read(res["log"]))[:40], "notes": notes,
               "limits": "No live HUD hidden (W_PlayerHUD_Main is not in the viewport). PrintString is not logged: read state back instead.",
               "run": res["run"], "seconds": res["seconds"], "timed_out": res["timed_out"]}
        if not ok:
            out["error"] = res.get("error") or ("%d step(s) failed" % failed if failed else "the run ended before all steps finished")
            if res.get("log_tail"):
                out["log_tail"] = res["log_tail"]
        return _out(out)

    # ---------------------------------------------------------------- hidden editor
    def t_hidden_editor_shot(args):
        mod = args["mod"]
        assets = list(args.get("assets") or [])
        if not assets or len(assets) > 8:
            return _out({"error": "give 1 to 8 assets"})
        for a in assets:
            bad = _in_mod(a, mod)
            if bad:
                return _out({"error": "%s: %s" % (a, bad)})
        stage = staging.load(mod)
        res = hiddendesk.run_editor(stage, {"op": "shots", "assets": assets, "max_width": int(args.get("max_width") or 1600),
                                            "settle_seconds": float(args.get("settle_seconds") or 9)}, timeout=420)
        content, saved = [], []
        for p in res.get("shots") or []:
            if os.path.isfile(p):
                data = Path(p).read_bytes()
                saved.append(p)
                content.append({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"})
        info = {"ok": res["ok"] and bool(saved), "shots": saved, "assets": (res.get("result") or []), "run": res["run"], "seconds": res["seconds"]}
        if not info["ok"]:
            info["error"] = res.get("error") or "no screenshots were captured"
            if res.get("log_tail"):
                info["log_tail"] = res["log_tail"]
        content.append({"type": "text", "text": json.dumps(info, indent=2, default=str)})
        return {"content": content, "isError": not info["ok"]}

    def t_mod_audit(args):
        mod = args["mod"]
        stage = staging.load(mod)
        staging.strip_tooling(stage)
        res = hiddendesk.run_editor(stage, {"op": "audit"}, timeout=float(args.get("timeout_seconds") or 420))
        log = _read(res["log"])
        audit = headless.parse_audit_log(log)
        comp = visible_compile(headless.parse_compile_log(log, mod))
        ok = bool(res["ok"]) and audit["completed"] and audit["errors"] == 0
        out = {"ok": ok, "completed": audit["completed"], "errors": audit["errors"], "warnings": audit["warnings"],
               "audited": audit["audited"], "lines": audit["lines"][-60:], "compile": comp, "run": res["run"], "seconds": res["seconds"],
               "note": "This is the editor's own upload Asset Audit run on the staged copy without the test harness: the real gate."}
        if not ok:
            out["error"] = res.get("error") or ("the audit did not complete" if not audit["completed"] else "%d audit error(s)" % audit["errors"])
            if res.get("log_tail"):
                out["log_tail"] = res["log_tail"]
        return _out(out)

    # ---------------------------------------------------------------- publish
    def t_bp_publish(args):
        mod = args["mod"]
        stage = staging.load(mod)
        editor = LiveEditor(run_json, remote_errors, py_status, mod, stage.meta().get("source_dir"), list_processes)

        def gate(packages):
            uassets = [p for p in packages]
            if not uassets:
                return []
            res = headless.run_commandlet(stage, {"op": "compile", "asset_paths": uassets}, timeout=600)
            if not res["ok"]:
                return [{"asset": None, "message": "the compile check could not run: %s" % (res.get("error") or "unknown")}]
            errs = list(visible_compile(headless.parse_compile_log(_read(res["log"]), mod))["errors"])
            for u in unchecked(res.get("result")):
                errs.append({"asset": u.get("asset"), "message": "could not be checked (%s): the compile gate fails closed" % (u.get("reason") or "unreadable")})
            return errs

        return _out(staging.publish(stage, editor, gate, assets=args.get("assets") or None, dry_run=bool(args.get("dry_run"))))

    # ---------------------------------------------------------------- table
    return [
        ("staging_sync", t_staging_sync,
         "Copy a mod project folder (the one holding the .uplugin) into a private staging area. Every headless tool works on that copy; "
         "nothing in the real mod changes until bp_publish. Refuses when staging holds unpublished changes unless discard_changes.",
         dict(mod_dir=STR, discard_changes=BOOL), ["mod_dir"]),
        ("staging_status", t_staging_status,
         "What differs between the staged copy and its baseline (changed, added, removed files), conflicts with the real mod, last sync time.",
         dict(mod=STR), ["mod"]),
        ("bp_apply", t_bp_apply,
         "Create or edit Blueprints headlessly on the staged copy, with no editor window and no mouse or keyboard. A JSON batch of asset specs: "
         "variables, functions, nodes and wires, components, widget tree, macros, dispatchers, interfaces, timelines, widget animations, raw T3D. "
         "Generated nodes are placed below existing ones. Compile errors roll the staged files back (unless keep_on_error). See the README spec reference.",
         dict(mod=STR, assets={"type": "array", "items": OBJ}, spec_file=STR, keep_on_error=BOOL, timeout_seconds=NUM), ["mod"]),
        ("bp_read", t_bp_read,
         "Structure of a staged mod Blueprint: variables, functions, macros, dispatchers, timelines, interfaces, components, widget tree, "
         "graphs with nodes, positions and resolved links. Refuses /Game. "
         "Components include the default root and inherited ones (flagged). components is null and components_status \"unavailable\" "
         "when they could not be read; [] means there really are none.",
         dict(mod=STR, asset_path=STR, graph=STR, include_pins=BOOL, timeout_seconds=NUM), ["mod", "asset_path"]),
        ("bp_compile_report", t_bp_compile_report,
         "Compile staged Blueprints (all of them when asset_paths is empty) and report errors and warnings per asset. Nothing is saved.",
         dict(mod=STR, asset_paths={"type": "array", "items": STR}, timeout_seconds=NUM), ["mod"]),
        ("bp_game_test", t_bp_game_test,
         "Run steps in a hidden game (-game -nullrhi, /Game/Maps/Empty by default): new/spawn/widget, call, get, set, find, wait, assert, python. "
         "Limits: cannot test anything injected into W_PlayerHUD_Main (no live HUD hidden); Python cannot spawn actors or create widgets in -game, "
         "so a harness Blueprint is built in the staged mod (Content/_McpTest, never published); PrintString is not logged in this build, "
         "so read state back with get steps and asserts.",
         dict(mod=STR, steps={"type": "array", "items": OBJ}, map=STR, timeout_seconds=NUM), ["mod", "steps"]),
        ("hidden_editor_shot", t_hidden_editor_shot,
         "Open up to 8 staged assets in an editor on a hidden Windows desktop and return screenshots of their windows. Never touches your desktop. "
         "Depends on the Mod Browser tile position of this editor build (fragile after a kit update; ABF_PICKER_TILE overrides): on failure it "
         "returns a clear error and a capture of the picker.",
         dict(mod=STR, assets={"type": "array", "items": STR}, max_width=INT, settle_seconds=NUM), ["mod", "assets"]),
        ("mod_audit", t_mod_audit,
         "Run the editor's own upload Asset Audit on the staged copy (without the test harness) in a hidden editor. This is the real gate and "
         "outranks blueprint_filter_check. Depends on the same fixed Mod Browser tile position as hidden_editor_shot.",
         dict(mod=STR, timeout_seconds=NUM), ["mod"]),
        ("bp_publish", t_bp_publish,
         "Copy the staged Content changes into the real mod. Backs up the files it replaces first (outside the mod), refuses on compile errors, "
         "modified assets in an open editor, locked files or files changed in the real mod since staging_sync, and never deletes anything. "
         "Works with the editor closed (file copy) or open (unload, copy, reload). dry_run only reports.",
         dict(mod=STR, assets={"type": "array", "items": STR}, dry_run=BOOL), ["mod"]),
    ]
