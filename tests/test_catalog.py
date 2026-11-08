"""Tests for the catalog tool and its pure helpers: no editor, no network, no real mod. Run with  python -I -m unittest tests.test_catalog -v

Same bootstrap as tests/test_abfc_backend.py.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "editor"))

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="abfcatalog")
    os.environ["ABF_HEADLESS_DIR"] = _TMP


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


import abfc_tools  # noqa: E402
import headless  # noqa: E402
import staging  # noqa: E402
from bpkit import catalogparse as cp  # noqa: E402


class TagTests(unittest.TestCase):
    def test_class_path_from_a_quoted_tag(self):
        self.assertEqual(cp.tag_class_path("/Script/CoreUObject.Class'/Script/UMG.UserWidget'"), "/Script/UMG.UserWidget")
        self.assertEqual(cp.tag_class_path("/Script/CoreUObject.Class'/Game/Fake/BP_Base.BP_Base_C'"), "/Game/Fake/BP_Base.BP_Base_C")

    def test_empty_and_none_give_none(self):
        for v in ("", "None", "  ", None):
            self.assertIsNone(cp.tag_class_path(v))

    def test_text_without_quotes_is_kept(self):
        self.assertEqual(cp.tag_class_path("/Script/Fake.Thing"), "/Script/Fake.Thing")

    def test_game_entry_interface_flag(self):
        e = cp.game_entry("/Game/Fake/BP_Fake", "BP_Fake_C", "BlueprintGeneratedClass",
                          "/Script/CoreUObject.Class'/Script/Fake.Thing'", str(0x4000 | 0x1))
        self.assertEqual(e, {"path": "/Game/Fake/BP_Fake.BP_Fake_C", "kind": "BlueprintGeneratedClass",
                             "parent": "/Script/Fake.Thing", "interface": True})
        e = cp.game_entry("/Game/Fake/W", "W_C", "WidgetBlueprintGeneratedClass", "None", "1")
        self.assertFalse(e["interface"])
        self.assertIsNone(e["parent"])

    def test_game_entry_unparsable_flags_are_not_an_interface(self):
        for flags in ("", "None", "abc", None):
            self.assertFalse(cp.game_entry("/Game/A", "A_C", "BlueprintGeneratedClass", "", flags)["interface"])
        self.assertEqual(cp.INTERFACE_FLAG, 0x4000)


class ParentTests(unittest.TestCase):
    def test_nearest_parent_skips_the_path_itself(self):
        self.assertEqual(cp.nearest_parent("/Script/F.C", ["/Script/F.C", "/Script/F.B", "/Script/F.A"]), "/Script/F.B")

    def test_nearest_parent_without_self(self):
        self.assertEqual(cp.nearest_parent("/Script/F.C", ["/Script/F.B", "/Script/F.A"]), "/Script/F.B")

    def test_nearest_parent_none_when_only_self_or_empty(self):
        self.assertIsNone(cp.nearest_parent("/Script/F.C", ["/Script/F.C"]))
        self.assertIsNone(cp.nearest_parent("/Script/F.C", []))
        self.assertIsNone(cp.nearest_parent("/Script/F.C", None))


class ScriptEntryTests(unittest.TestCase):
    def test_plain_class(self):
        self.assertEqual(cp.script_entry("/Script/F.Thing", "/Script/F.Base", False, ["Ignored"]),
                         {"path": "/Script/F.Thing", "parent": "/Script/F.Base"})

    def test_interface_by_parent(self):
        e = cp.script_entry("/Script/F.IThing", "/Script/CoreUObject.Interface", False, [])
        self.assertTrue(e["interface"])
        self.assertNotIn("library", e)

    def test_library_functions_are_filtered_sorted_and_unique(self):
        e = cp.script_entry("/Script/F.Lib", "/Script/Engine.BlueprintFunctionLibrary", True,
                            ["Zed", "Alpha", "Alpha", "K2_Beta", "bad name", "1Bad", "", "with.dot", "_ok", None, 5])
        self.assertTrue(e["library"])
        self.assertEqual(e["functions"], ["Alpha", "K2_Beta", "Zed", "_ok"])
        self.assertNotIn("interface", e)

    def test_library_without_functions(self):
        self.assertEqual(cp.script_entry("/Script/F.Lib", None, True, None)["functions"], [])


class AssembleTests(unittest.TestCase):
    def doc(self, errors=()):
        game = [{"path": "/Game/B.B_C", "kind": "BlueprintGeneratedClass", "parent": None, "interface": False},
                {"path": "/Game/A.A_C", "kind": "BlueprintGeneratedClass", "parent": None, "interface": True}]
        classes = [cp.script_entry("/Script/F.Z", None, False, []),
                   cp.script_entry("/Script/F.Lib", None, True, ["Two", "One"]),
                   cp.script_entry("/Script/F.A", None, False, [])]
        return cp.assemble("5.4.4", game, classes, ["/Script/F.S2", "/Script/F.S1"], ["/Script/F.E"], list(errors))

    def test_sorted_and_counted(self):
        d = self.doc()
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["engineVersion"], "5.4.4")
        self.assertEqual([g["path"] for g in d["game"]], ["/Game/A.A_C", "/Game/B.B_C"])
        self.assertEqual([c["path"] for c in d["scriptClasses"]], ["/Script/F.A", "/Script/F.Lib", "/Script/F.Z"])
        self.assertEqual(d["scriptStructs"], ["/Script/F.S1", "/Script/F.S2"])
        self.assertEqual(d["scriptEnums"], ["/Script/F.E"])
        self.assertEqual(d["counts"], {"game": 2, "scriptClasses": 3, "scriptStructs": 2, "scriptEnums": 1, "libraries": 1, "functions": 2})

    def test_errors_are_capped(self):
        d = self.doc(["e%d" % i for i in range(500)])
        self.assertEqual(len(d["errors"]), 200)
        self.assertEqual(d["errors"][0], "e0")
        self.assertEqual(self.doc()["errors"], [])

    def test_document_is_json(self):
        json.dumps(self.doc(), separators=(",", ":"))


class CatalogToolTests(unittest.TestCase):
    def tool(self):
        return [t for t in abfc_tools.build("x") if t[0] == "catalog"][0]

    def test_schema(self):
        name, fn, desc, props, required = self.tool()
        self.assertEqual(required, ["mod"])
        self.assertEqual(sorted(props), ["mod", "timeout_seconds"])
        self.assertIn("catalog", abfc_tools.FEATURES)

    def test_success_shape(self):
        stage = object()
        res = {"ok": True, "run": os.path.join(_TMP, "runs", "r1"), "seconds": 7.5, "warm": True,
               "result": {"file": "catalog.json", "counts": {"game": 3, "scriptClasses": 4}}}
        with mock.patch.object(staging, "load", return_value=stage) as load, \
                mock.patch.object(headless, "run_commandlet", return_value=res) as run, \
                mock.patch.object(staging, "strip_scratch") as strip:
            text, is_err = self.tool()[1]({"mod": "TestMod"})
        self.assertFalse(is_err)
        d = json.loads(text)
        self.assertEqual(d, {"ok": True, "run": res["run"], "seconds": 7.5, "file": str(Path(res["run"]) / "catalog.json"),
                             "counts": {"game": 3, "scriptClasses": 4}, "warm": True})
        load.assert_called_once_with("TestMod")
        self.assertEqual(run.call_args.args[:2], (stage, {"op": "catalog"}))
        self.assertEqual(run.call_args.kwargs["timeout"], 300.0)
        strip.assert_called_once_with(stage)

    def test_timeout_argument_and_cold_run(self):
        res = {"ok": True, "run": "r", "seconds": 1, "result": {"counts": {}}}
        with mock.patch.object(staging, "load"), mock.patch.object(headless, "run_commandlet", return_value=res) as run, \
                mock.patch.object(staging, "strip_scratch"):
            text, _ = self.tool()[1]({"mod": "TestMod", "timeout_seconds": 42})
        self.assertEqual(run.call_args.kwargs["timeout"], 42.0)
        self.assertIs(json.loads(text)["warm"], False)

    def test_error_shape_and_scratch_is_stripped(self):
        stage = object()
        res = {"ok": False, "run": "r2", "error": "boom", "log_tail": ["x"]}
        with mock.patch.object(staging, "load", return_value=stage), mock.patch.object(headless, "run_commandlet", return_value=res), \
                mock.patch.object(staging, "strip_scratch") as strip:
            text, is_err = self.tool()[1]({"mod": "TestMod"})
        self.assertTrue(is_err)
        self.assertEqual(json.loads(text), {"error": "boom", "run": "r2", "log_tail": ["x"]})
        strip.assert_called_once_with(stage)

    def test_strip_scratch_runs_when_the_run_raises(self):
        stage = object()
        with mock.patch.object(staging, "load", return_value=stage), \
                mock.patch.object(headless, "run_commandlet", side_effect=headless.HeadlessError("no editor")), \
                mock.patch.object(staging, "strip_scratch") as strip:
            with self.assertRaises(headless.HeadlessError):
                self.tool()[1]({"mod": "TestMod"})
        strip.assert_called_once_with(stage)

    def test_bad_mod_is_refused_without_a_run(self):
        for bad in (None, "", 5, "a/b", "..", "a b", "Mod;x", ["Mod"]):
            with mock.patch.object(staging, "load") as load, mock.patch.object(headless, "run_commandlet") as run:
                text, is_err = self.tool()[1]({"mod": bad})
            self.assertTrue(is_err, bad)
            self.assertIn("error", json.loads(text))
            load.assert_not_called()
            run.assert_not_called()
        with mock.patch.object(staging, "load") as load:
            text, is_err = self.tool()[1]({})
        self.assertTrue(is_err)
        load.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_catalog_is_a_warm_op(self):
        self.assertIn("catalog", headless.WARM_OPS)

    def test_jobs_registers_the_op(self):
        text = (ROOT / "editor" / "bpkit" / "jobs.py").read_text(encoding="utf-8")
        self.assertIn('"catalog": op_catalog', text)
        self.assertIn("def op_catalog(job):", text)


if __name__ == "__main__":
    unittest.main()
