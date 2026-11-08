"""Tests for bp_asset_ops: validation, host snapshot/restore, recreate and the editor-side ops with a fake `unreal`.
No editor, no network, no real mod. Imported by tests/test_abfc_backend.py so  python -m unittest tests.test_abfc_backend  runs them.
"""
import json
import os
import py_compile
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "editor"))

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="abfaopsroot")
    os.environ["ABF_HEADLESS_DIR"] = _TMP


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


import headless  # noqa: E402


class AssetOpsSpecTests(unittest.TestCase):
    """specs.check_asset_ops: the pure validation of a bp_asset_ops request."""
    MOD = "TestMod"

    def check(self, ops):
        from bpkit import specs
        return specs.check_asset_ops(ops, self.MOD)

    def test_valid_ops_pass(self):
        self.assertEqual(self.check([
            {"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"},
            {"op": "delete", "asset": "/TestMod/A/BP_C"},
            {"op": "reparent", "asset": "/TestMod/A/BP_D", "parent": "/Script/Engine.Pawn"},
            {"op": "reparent", "asset": "/TestMod/A/BP_D", "parent": "/TestMod/A/BP_Base.BP_Base_C"},
            {"op": "recreate", "asset": "/TestMod/A/BP_E"}]), [])

    def test_ops_list_shape(self):
        for bad in ([], None, "rename", ["rename"]):
            self.assertTrue(self.check(bad), bad)

    def test_unknown_op_and_unknown_key(self):
        self.assertIn("must be one of", " ".join(self.check([{"op": "move", "asset": "/TestMod/A/BP_A"}])))
        self.assertIn("unknown key", " ".join(self.check([{"op": "delete", "asset": "/TestMod/A/BP_A", "force": True}])))

    def test_missing_keys(self):
        self.assertTrue(self.check([{"op": "rename", "from": "/TestMod/A/BP_A"}]))
        self.assertTrue(self.check([{"op": "delete"}]))
        self.assertTrue(self.check([{"op": "reparent", "asset": "/TestMod/A/BP_A"}]))
        self.assertTrue(self.check([{"op": "recreate", "asset": ""}]))

    def test_game_and_foreign_roots_are_refused(self):
        for bad in ("/Game/Maps/BP_A", "/Game", "/OtherMod/A/BP_A", "/Script/Engine.Actor", "TestMod/A/BP_A"):
            self.assertTrue(self.check([{"op": "delete", "asset": bad}]), bad)
            self.assertTrue(self.check([{"op": "rename", "from": "/TestMod/A/BP_A", "to": bad}]), bad)

    def test_config_assets_and_tooling_are_refused(self):
        for bad in ("/TestMod/AbioticModConfig", "/TestMod/AbioticModConfig/Thing", "/TestMod/UUGC_Metadata",
                    "/TestMod/_McpTest/BP_McpHarness", "/TestMod/_McpTmp/BP_X", "/TestMod/A/_McpTmp/BP_X"):
            self.assertTrue(self.check([{"op": "delete", "asset": bad}]), bad)
            self.assertTrue(self.check([{"op": "recreate", "asset": bad}]), bad)

    def test_path_characters(self):
        for bad in ("/TestMod/A/BP A", "/TestMod/A/../BP_A", "/TestMod/A/BP_A.BP_A", "/TestMod/A//BP_A", "/TestMod/A/BP_A/",
                    "/TestMod/A/BP'A", "/TestMod/"):
            self.assertTrue(self.check([{"op": "delete", "asset": bad}]), bad)

    def test_rename_to_itself_is_refused(self):
        self.assertTrue(self.check([{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/A/BP_A"}]))

    def test_parent_class_paths(self):
        for bad in ("/Script/Engine", "Engine.Actor", "/Game/../Evil.Evil_C", "/Script/Engine.Actor'", "/Other/X.X_C",
                    "/Script/Engine/Sub.Actor", "/TestMod/AbioticModConfig/Thing.Thing_C", ""):
            self.assertTrue(self.check([{"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": bad}]), bad)
        self.assertEqual(self.check([{"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Game/Blueprints/BP_Base.BP_Base_C"}]), [])

    def test_at_most_twenty_ops(self):
        ops = [{"op": "delete", "asset": "/TestMod/A/BP_%d" % i} for i in range(20)]
        self.assertEqual(self.check(ops), [])
        self.assertIn("at most 20", " ".join(self.check(ops + [{"op": "delete", "asset": "/TestMod/A/BP_X"}])))

    def test_bp_apply_still_refuses_reparent(self):
        from bpkit import specs
        problems = specs.validate({"assets": [{"asset": "/TestMod/A/BP_A", "reparent": "/Script/Engine.Pawn"}]}, self.MOD)
        self.assertIn("reparenting is not offered", " ".join(problems))


class AssetOpsHostTests(unittest.TestCase):
    """The bp_asset_ops tool on the host: validation, ordering, recreate, snapshot and restore. The commandlet is faked."""
    MOD = "TestMod"

    def setUp(self):
        import headless_tools
        self.ht = headless_tools
        self.dir = Path(tempfile.mkdtemp(prefix="abfaops"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.stage = SimpleNamespace(dir=self.dir, name=self.MOD)
        self.calls = []
        self.tools = {t[0]: t for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                              ROOT / "editor", [], "", list_processes=lambda: [])}
        self.write("Content/A/BP_A.uasset", b"A-UASSET")
        self.write("Content/A/BP_A.uexp", b"A-UEXP")
        self.write("Content/A/BP_AB.uasset", b"AB-UASSET")
        self.write("Content/A/BP_R.uasset", b"R-UASSET")
        self.write("Config/Tags/TestModTags.ini", b"[X]\r\n")

    def write(self, rel, data):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def files(self):
        return sorted(p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*") if p.is_file())

    def call(self, ops, commandlet=None):
        def fake(stage, job, timeout=300, prepare=None):
            self.calls.append(job)
            return commandlet(job) if commandlet else {"ok": True, "result": {"ok": True, "results": [], "touched": []}, "run": "run1", "seconds": 1.0}
        with mock.patch.object(self.ht.staging, "load", return_value=self.stage), \
                mock.patch.object(headless, "run_commandlet", side_effect=fake):
            text, is_err = self.tools["bp_asset_ops"][1]({"mod": self.MOD, "ops": ops})
        return json.loads(text), is_err

    def rename_ok(self, job):
        for op in job["ops"]:
            old, new = op["from"], op["to"]
            for suffix in (".uasset", ".uexp"):
                src = self.dir / ("Content/" + old[len("/TestMod/"):] + suffix)
                if src.is_file():
                    dst = self.dir / ("Content/" + new[len("/TestMod/"):] + suffix)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
        self.write("Content/A/BP_R.uasset", b"R-FIXED")
        return {"ok": True, "result": {"ok": True, "results": [{"op": "rename", "ok": True, "detail": "renamed"}],
                                       "touched": ["/TestMod/A/BP_R"]}, "run": "run-rename", "seconds": 2.0}

    def test_tool_is_registered_with_a_schema(self):
        name, fn, desc, props, required = self.tools["bp_asset_ops"]
        self.assertEqual(required, ["mod", "ops"])
        self.assertEqual(props["ops"]["type"], "array")
        self.assertIn("STAGED", desc)
        self.assertIn('"asset_ops": op_asset_ops', (ROOT / "editor" / "bpkit" / "jobs.py").read_text(encoding="utf-8"))

    def test_invalid_ops_never_start_a_run(self):
        before = self.files()
        for ops in ([{"op": "delete", "asset": "/Game/X"}], [{"op": "rename", "from": "/TestMod/A/BP_A"}],
                    [{"op": "delete", "asset": "/TestMod/A/BP_%d" % i} for i in range(21)]):
            d, is_err = self.call(ops)
            self.assertTrue(is_err)
            self.assertIs(d["ok"], False)
            self.assertTrue(d["problems"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.files(), before)

    def test_missing_source_and_existing_target_are_refused_before_the_run(self):
        d, is_err = self.call([{"op": "rename", "from": "/TestMod/A/BP_Missing", "to": "/TestMod/A/BP_New"}])
        self.assertTrue(is_err)
        self.assertIn("not in the staged mod", " ".join(d["problems"]))
        d, is_err = self.call([{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/A/BP_AB"}])
        self.assertIn("already exists", " ".join(d["problems"]))
        d, is_err = self.call([{"op": "delete", "asset": "/TestMod/A/BP_Missing"}])
        self.assertIn("not in the staged mod", " ".join(d["problems"]))
        self.assertEqual(self.calls, [])

    def test_rename_runs_the_asset_ops_op_and_reports_touched(self):
        d, is_err = self.call([{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"}], self.rename_ok)
        self.assertFalse(is_err)
        self.assertIs(d["ok"], True)
        self.assertEqual(d["touched"], ["/TestMod/A/BP_R"])
        self.assertEqual(d["run"], "run-rename")
        self.assertEqual(d["results"][0]["op"], "rename")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["op"], "asset_ops")
        self.assertEqual(self.calls[0]["ops"], [{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"}])
        self.assertIn("Content/B/BP_B.uasset", self.files())
        self.assertNotIn("Content/A/BP_A.uasset", self.files())
        self.assertEqual(list((self.ht.staging.root() / "snapshots").glob("TestMod-*")), [])      # the snapshot is dropped on success

    def test_failing_commandlet_restores_everything(self):
        before = {rel: (self.dir / rel).read_bytes() for rel in self.files()}

        def failing(job):
            (self.dir / "Content/A/BP_A.uasset").unlink()
            self.write("Content/A/BP_Half.uasset", b"HALF")
            (self.dir / "Config/Tags/TestModTags.ini").write_bytes(b"changed")
            return {"ok": True, "result": {"ok": False, "results": [{"op": "rename", "ok": False, "detail": "boom"}], "touched": ["/TestMod/A/BP_R"],
                                           "error": "rename: boom"}, "run": "run-fail", "seconds": 1.0}
        d, is_err = self.call([{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"}], failing)
        self.assertTrue(is_err)
        self.assertIs(d["ok"], False)
        self.assertIs(d["rolled_back"], True)
        self.assertEqual(d["touched"], [])
        self.assertIn("rename: boom", d["error"])
        self.assertEqual({rel: (self.dir / rel).read_bytes() for rel in self.files()}, before)

    def test_crashed_commandlet_restores_everything(self):
        before = {rel: (self.dir / rel).read_bytes() for rel in self.files()}

        def crashed(job):
            (self.dir / "Content/A/BP_A.uasset").write_bytes(b"torn")
            return {"ok": False, "result": None, "error": "no result file: the hidden process crashed", "run": "run-crash", "seconds": 1.0}
        d, is_err = self.call([{"op": "delete", "asset": "/TestMod/A/BP_A"}], crashed)
        self.assertTrue(is_err)
        self.assertIs(d["rolled_back"], True)
        self.assertEqual({rel: (self.dir / rel).read_bytes() for rel in self.files()}, before)

    def test_leftover_redirector_is_a_failure(self):
        def leaves_old(job):
            self.write("Content/B/BP_B.uasset", b"A-UASSET")          # copied, the old file stays
            return {"ok": True, "result": {"ok": True, "results": [], "touched": []}, "run": "r", "seconds": 1.0}
        d, is_err = self.call([{"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"}], leaves_old)
        self.assertTrue(is_err)
        self.assertIn("still", d["error"])
        self.assertIs(d["rolled_back"], True)
        self.assertNotIn("Content/B/BP_B.uasset", self.files())
        self.assertIn("Content/A/BP_A.uasset", self.files())

    def test_recreate_deletes_only_that_assets_files_and_needs_no_run(self):
        d, is_err = self.call([{"op": "recreate", "asset": "/TestMod/A/BP_A"}])
        self.assertFalse(is_err)
        self.assertIs(d["ok"], True)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.files(), ["Config/Tags/TestModTags.ini", "Content/A/BP_AB.uasset", "Content/A/BP_R.uasset"])
        self.assertEqual(d["results"][0]["op"], "recreate")

    def test_recreate_of_a_missing_asset_is_not_an_error(self):
        d, is_err = self.call([{"op": "recreate", "asset": "/TestMod/A/BP_Gone"}])
        self.assertFalse(is_err)
        self.assertIn("removed 0", d["results"][0]["detail"])

    def test_ops_run_in_order_around_a_recreate(self):
        order = []

        def fake(job):
            order.append((" ".join(o["op"] for o in job["ops"]), list(job["delete_set"])))
            for o in job["ops"]:
                if o["op"] == "delete":
                    (self.dir / ("Content/" + o["asset"][len("/TestMod/"):] + ".uasset")).unlink()
            return {"ok": True, "result": {"ok": True, "results": [{"op": o["op"], "ok": True, "detail": ""} for o in job["ops"]], "touched": []},
                    "run": "r%d" % len(order), "seconds": 1.0}
        ops = [{"op": "delete", "asset": "/TestMod/A/BP_R"}, {"op": "recreate", "asset": "/TestMod/A/BP_A"},
               {"op": "reparent", "asset": "/TestMod/A/BP_AB", "parent": "/Script/Engine.Pawn"}]
        d, _ = self.call(ops, fake)
        self.assertEqual(d.get("error"), None)
        self.assertEqual(order, [("delete", ["/TestMod/A/BP_R"]), ("reparent", ["/TestMod/A/BP_R"])])
        self.assertEqual([r["op"] for r in d["results"]], ["delete", "recreate", "reparent"])
        self.assertEqual(d["run"], "r2")


class AssetOpsEditorSideTests(unittest.TestCase):
    """editor/bpkit/assetops.py with a fake in-memory `unreal` asset world."""
    MOD = "TestMod"

    class Cls:
        def __init__(self, path, family):
            self.path = path
            self.family = family

        def get_path_name(self):
            return self.path

    class Asset:
        def __init__(self, pkg, kind="Blueprint", parent=None):
            self.pkg = pkg
            self.kind = kind
            self.parent = parent
            self.dirty = False

        def get_class(self):
            return SimpleNamespace(get_name=lambda: self.kind)

        def get_package(self):
            return SimpleNamespace(is_dirty=lambda: self.dirty)

        def get_editor_property(self, key):
            # live: a Blueprint's parent_class cannot be read from Python
            raise RuntimeError("Blueprint: Failed to find property '%s' for attribute '%s' on 'Blueprint'" % (key, key))

        def generated_class(self):
            return self.parent

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)
        import bpkit
        import importlib
        for short in ("core", "assetops"):
            name = "bpkit." + short
            sys.modules.pop(name, None)
            if hasattr(bpkit, short):
                delattr(bpkit, short)
            self.addCleanup(sys.modules.pop, name, None)
            self.addCleanup(lambda s=short: hasattr(bpkit, s) and delattr(bpkit, s))
        self.assetops = importlib.import_module("bpkit.assetops")
        self.u = u = sys.modules["unreal"]
        self.world, self.refs, self.saved, self.fixups = {}, {}, [], []
        self.leave_redirector = True
        self.has_fixup = True
        self.classes = {
            "/Script/Engine.Actor": self.Cls("/Script/Engine.Actor", "actor"),
            "/Script/Engine.Pawn": self.Cls("/Script/Engine.Pawn", "actor"),
            "/Script/Engine.ActorComponent": self.Cls("/Script/Engine.ActorComponent", "component"),
            "/Script/Engine.Object": self.Cls("/Script/Engine.Object", "object")}
        u.Actor.static_class.return_value = "actor"
        u.ActorComponent.static_class.return_value = "component"
        u.UserWidget.static_class.return_value = "widget"
        u.MathLibrary.class_is_child_of.side_effect = lambda cls, base: cls.family == base or cls is base
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_referencers.side_effect = lambda pkg, opts: list(self.refs.get(pkg, []))
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_asset_by_object_path.side_effect = lambda obj: SimpleNamespace(
            get_tag_value=lambda name: "/Script/CoreUObject.Class'%s'" % self.world[obj.split(".")[0]].parent.path)
        lib = u.EditorAssetLibrary
        lib.does_asset_exist.side_effect = lambda obj: obj.split(".")[0] in self.world
        lib.rename_asset.side_effect = self.rename_asset
        lib.delete_asset.side_effect = lambda obj: self.world.pop(obj.split(".")[0], None) is not None
        lib.save_loaded_asset.side_effect = self.save_loaded_asset
        u.load_asset.side_effect = lambda obj: self.world.get(obj.split(".")[0])
        u.load_class.side_effect = lambda outer, path: self.classes.get(path)
        tools = SimpleNamespace(fixup_referencers=self.fixup_referencers)
        u.AssetToolsHelpers.get_asset_tools.side_effect = lambda: tools if self.has_fixup else SimpleNamespace()
        u.BlueprintEditorLibrary.reparent_blueprint.side_effect = self.reparent_blueprint

    def rename_asset(self, old, new):
        old, new = old.split(".")[0], new.split(".")[0]
        if old not in self.world or new in self.world:
            return False
        self.world[new] = self.world.pop(old)
        self.world[new].pkg = new
        if old in self.refs:
            self.refs[new] = self.refs.pop(old)
        for r in self.refs.get(new, []):
            self.world[r].dirty = True
        if self.leave_redirector:
            self.world[old] = self.Asset(old, kind="ObjectRedirector")
        return True

    def fixup_referencers(self, redirectors):
        self.fixups.append([r.pkg for r in redirectors])
        for r in redirectors:
            self.world.pop(r.pkg, None)

    def save_loaded_asset(self, asset, only_if_dirty):
        if only_if_dirty and not asset.dirty:
            return True
        asset.dirty = False
        self.saved.append(asset.pkg)
        return True

    def reparent_blueprint(self, bp, cls):
        bp.parent = cls

    def add(self, pkg, **kw):
        self.world[pkg] = self.Asset(pkg, **kw)
        return self.world[pkg]

    def exec_ops(self, *ops, **extra):
        job = {"mod": self.MOD, "ops": list(ops)}
        job.update(extra)
        return self.assetops.run(job)

    def test_rename_fixes_referencers_and_removes_the_redirector(self):
        self.add("/TestMod/A/BP_A")
        self.add("/TestMod/A/BP_R")
        self.refs["/TestMod/A/BP_A"] = ["/TestMod/A/BP_R"]
        res = self.exec_ops({"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"})
        self.assertIs(res["ok"], True, res)
        self.assertEqual(self.fixups, [["/TestMod/A/BP_A"]])
        self.assertNotIn("/TestMod/A/BP_A", self.world)
        self.assertIn("/TestMod/B/BP_B", self.world)
        self.assertEqual(res["touched"], ["/TestMod/A/BP_R"])
        self.assertIn("/TestMod/A/BP_R", self.saved)
        self.assertIn("/TestMod/B/BP_B", self.saved)
        self.u.SystemLibrary.collect_garbage.assert_called()

    def test_rename_without_a_redirector_needs_no_fixup(self):
        self.leave_redirector = False
        self.add("/TestMod/A/BP_A")
        res = self.exec_ops({"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"})
        self.assertIs(res["ok"], True, res)
        self.assertEqual(self.fixups, [])
        self.assertEqual(res["touched"], [])

    def test_rename_refuses_an_existing_target_and_a_missing_source(self):
        self.add("/TestMod/A/BP_A")
        self.add("/TestMod/A/BP_B")
        res = self.exec_ops({"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/A/BP_B"})
        self.assertIs(res["ok"], False)
        self.assertIn("already exists", res["error"])
        self.assertIn("/TestMod/A/BP_A", self.world)
        res = self.exec_ops({"op": "rename", "from": "/TestMod/A/BP_Gone", "to": "/TestMod/A/BP_C"})
        self.assertIn("does not exist", res["error"])

    def test_rename_without_fixup_support_is_a_clear_error(self):
        self.has_fixup = False
        self.add("/TestMod/A/BP_A")
        res = self.exec_ops({"op": "rename", "from": "/TestMod/A/BP_A", "to": "/TestMod/B/BP_B"})
        self.assertIs(res["ok"], False)
        self.assertIn("fixup_referencers", res["error"])

    def test_delete_is_refused_while_something_else_references_the_asset(self):
        self.add("/TestMod/A/BP_A")
        self.add("/TestMod/A/BP_R")
        self.refs["/TestMod/A/BP_A"] = ["/TestMod/A/BP_R"]
        res = self.exec_ops({"op": "delete", "asset": "/TestMod/A/BP_A"})
        self.assertIs(res["ok"], False)
        self.assertIn("/TestMod/A/BP_R", res["error"])
        self.assertIn("/TestMod/A/BP_A", self.world)

    def test_delete_allows_referencers_inside_the_delete_set(self):
        self.add("/TestMod/A/BP_A")
        self.add("/TestMod/A/BP_R")
        self.refs["/TestMod/A/BP_A"] = ["/TestMod/A/BP_R"]
        res = self.exec_ops({"op": "delete", "asset": "/TestMod/A/BP_R"}, {"op": "delete", "asset": "/TestMod/A/BP_A"})
        self.assertIs(res["ok"], True, res)
        self.assertEqual(self.world, {})
        self.assertEqual([r["op"] for r in res["results"]], ["delete", "delete"])

    def test_delete_with_a_delete_set_from_the_job(self):
        self.add("/TestMod/A/BP_A")
        self.refs["/TestMod/A/BP_A"] = ["/TestMod/A/BP_Later"]
        res = self.exec_ops({"op": "delete", "asset": "/TestMod/A/BP_A"}, delete_set=["/TestMod/A/BP_A", "/TestMod/A/BP_Later"])
        self.assertIs(res["ok"], True, res)

    def test_reparent_within_a_family(self):
        bp = self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Pawn"})
        self.assertIs(res["ok"], True, res)
        self.assertIs(bp.parent, self.classes["/Script/Engine.Pawn"])
        self.u.BlueprintEditorLibrary.compile_blueprint.assert_called_once()
        self.assertIn("/TestMod/A/BP_A", self.saved)

    def test_reparent_across_families_is_refused(self):
        bp = self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        for target in ("/Script/Engine.ActorComponent", "/Script/Engine.Object"):
            res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": target})
            self.assertIs(res["ok"], False)
            self.assertIn("family", res["error"])
        self.assertIs(bp.parent, self.classes["/Script/Engine.Actor"])
        self.u.BlueprintEditorLibrary.reparent_blueprint.assert_not_called()

    def test_reparent_to_the_same_parent_changes_nothing(self):
        self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Actor"})
        self.assertIs(res["ok"], True, res)
        self.u.BlueprintEditorLibrary.reparent_blueprint.assert_not_called()

    def test_reparent_unknown_class_and_non_blueprint(self):
        self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Nope"})
        self.assertIn("cannot be loaded", res["error"])
        self.add("/TestMod/A/DT_X", kind="DataTable")
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/DT_X", "parent": "/Script/Engine.Pawn"})
        self.assertIn("not a Blueprint", res["error"])

    def test_reparent_that_does_not_stick_is_an_error(self):
        self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        self.u.BlueprintEditorLibrary.reparent_blueprint.side_effect = lambda b, c: None
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Pawn"})
        self.assertIs(res["ok"], False)
        self.assertIn("does not derive", res["error"])

    def test_reparent_to_an_ancestor_that_does_nothing_is_an_error(self):
        # Pawn derives from Actor, so class_is_child_of(now, Actor) is true even when the reparent did not happen
        self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Pawn"])
        self.u.BlueprintEditorLibrary.reparent_blueprint.side_effect = lambda b, c: None
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Actor"})
        self.assertIs(res["ok"], False, res)
        self.assertIn("does not derive", res["error"])

    def test_reparent_to_an_ancestor_that_sticks_succeeds(self):
        bp = self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Pawn"])
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Actor"})
        self.assertIs(res["ok"], True, res)
        self.assertIs(bp.parent, self.classes["/Script/Engine.Actor"])

    def test_reparent_falls_back_to_the_class_check_when_the_tag_is_unreadable(self):
        self.add("/TestMod/A/BP_A", parent=self.classes["/Script/Engine.Actor"])
        self.u.AssetRegistryHelpers.get_asset_registry.return_value.get_asset_by_object_path.side_effect = RuntimeError("no registry")
        res = self.exec_ops({"op": "reparent", "asset": "/TestMod/A/BP_A", "parent": "/Script/Engine.Pawn"})
        self.assertIs(res["ok"], True, res)

    def test_the_first_failure_stops_the_run(self):
        self.add("/TestMod/A/BP_A")
        res = self.exec_ops({"op": "delete", "asset": "/TestMod/A/BP_Gone"}, {"op": "delete", "asset": "/TestMod/A/BP_A"})
        self.assertIs(res["ok"], False)
        self.assertEqual(len(res["results"]), 1)
        self.assertIn("/TestMod/A/BP_A", self.world)

    def test_recreate_never_reaches_the_editor(self):
        res = self.exec_ops({"op": "recreate", "asset": "/TestMod/A/BP_A"})
        self.assertIs(res["ok"], False)
        self.assertIn("host", res["error"])

    def test_editor_side_files_compile(self):
        tmp = Path(tempfile.mkdtemp(prefix="abfaops"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        for rel in ("editor/bpkit/assetops.py", "editor/bpkit/jobs.py", "editor/bpkit/specs.py"):
            py_compile.compile(str(ROOT / rel), cfile=str(tmp / "x.pyc"), doraise=True)


if __name__ == "__main__":
    unittest.main()
