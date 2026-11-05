"""Tests for the abfc extensions: no editor, no network, no real mod. Run with  python -m unittest tests.test_abfc_backend -v

Same bootstrap as tests/test_unit.py. Nothing here assumes backend_info exists or is absent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "editor"))

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="abfabfc")
    os.environ["ABF_HEADLESS_DIR"] = _TMP


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


import abfc_tools  # noqa: E402
import headless  # noqa: E402
from bpkit import reflectparse  # noqa: E402


class CompileLogTests(unittest.TestCase):
    SRC = "/Mod/X/BP_A.BP_A:EventGraph.K2Node_CallFunction_12"
    SRC2 = "/Mod/X/BP_A.BP_A:UpdateTarget.K2Node_VariableSet_3"

    def line(self, sev, msg, src=None):
        tail = " from Source: " + src if src else ""
        return "[2025.01.01-00.00.00:000][  0]LogBlueprint: %s: [Compiler] %s%s\n" % (sev, msg, tail)

    def test_source_gives_graph_and_node(self):
        r = headless.parse_compile_log(self.line("Error", "Bad pin", self.SRC))
        self.assertEqual(r["errors"], [{"asset": "/Mod/X/BP_A", "message": "Bad pin", "source": self.SRC,
                                        "graph": "EventGraph", "node": "K2Node_CallFunction_12"}])

    def test_two_messages_on_different_nodes_are_both_kept(self):
        log = self.line("Error", "Same text", self.SRC) + self.line("Error", "Same text", self.SRC2) + self.line("Error", "Same text", self.SRC)
        r = headless.parse_compile_log(log)
        self.assertEqual([(e["graph"], e["node"]) for e in r["errors"]],
                         [("EventGraph", "K2Node_CallFunction_12"), ("UpdateTarget", "K2Node_VariableSet_3")])

    def test_entry_without_source_is_exactly_asset_and_message(self):
        log = "[x][0]LogBlueprint: Warning: [AssetLog] /Mod/X/BP_A.BP_A: [Compiler] Something is odd\n"
        r = headless.parse_compile_log(log)
        self.assertEqual(r["warnings"], [{"asset": "/Mod/X/BP_A", "message": "Something is odd"}])


class ToolTableTests(unittest.TestCase):
    def test_build_returns_exactly_two_tools_with_schemas(self):
        tools = abfc_tools.build("x")
        self.assertEqual([t[0] for t in tools], ["abfc_extensions", "reflect"])
        for name, fn, desc, props, required in tools:
            self.assertTrue(callable(fn))
            self.assertTrue(desc)
            self.assertIsInstance(props, dict)
            self.assertTrue(set(required) <= set(props))
        reflect = tools[1]
        self.assertEqual(reflect[4], ["mod", "classes"])
        for k in ("classes", "structs", "cast_targets"):
            self.assertEqual(reflect[3][k]["type"], "array")
        self.assertEqual(tools[0][3], {})

    def test_abfc_extensions_shape(self):
        text, is_err = abfc_tools.build("9.9.9")[0][1]({})
        self.assertFalse(is_err)
        d = json.loads(text)
        self.assertEqual(d["extProtocol"], 1)
        self.assertEqual(d["serverVersion"], "9.9.9")
        self.assertEqual(d["python"], sys.version.split()[0])
        self.assertEqual(d["features"], ["reflect", "compile_node_refs", "bp_read_seconds"])
        self.assertEqual(sorted(d), ["extProtocol", "features", "python", "serverVersion"])

    def test_reflect_host_refuses_foreign_paths(self):
        reflect = abfc_tools.build("x")[1][1]
        for bad in ("/Engine/X", "/Other/Thing", "Script/Engine.Actor", "/Game/A'B", "/Game/../Etc"):
            text, is_err = reflect({"mod": "Mod", "classes": [bad]})
            self.assertTrue(is_err, bad)
            self.assertIn("error", json.loads(text))
        text, is_err = reflect({"mod": "Mod", "classes": ["/Script/Engine.Actor"], "structs": ["/Engine/S"]})
        self.assertTrue(is_err)
        self.assertIn("structs", json.loads(text)["error"])
        text, is_err = reflect({"mod": "Mod", "classes": [], "cast_targets": ["/Engine/C"]})
        self.assertTrue(is_err)


class ReflectParseTests(unittest.TestCase):
    PINS = [
        'CustomProperties Pin (PinId=AAAA0000000000000000000000000001,PinName="self",PinFriendlyName=NSLOCTEXT("K2Node", "Target", "Target"),'
        'PinType.PinCategory="object",PinType.PinSubCategory="",PinType.PinSubCategoryObject="/Script/CoreUObject.Class\'/Script/Fake.Widget\'",'
        'PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,PinType.bIsReference=False,'
        'PersistentGuid=00000000000000000000000000000000,bHidden=True,bNotConnectable=False,)',
        'CustomProperties Pin (PinId=AAAA0000000000000000000000000002,PinName="Items",Direction="EGPD_Output",'
        'PinType.PinCategory="wildcard",PinType.PinSubCategory="",PinType.PinSubCategoryObject=None,'
        'PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=Array,PinType.bIsReference=False,'
        'PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,)',
        'CustomProperties Pin (PinId=AAAA0000000000000000000000000003,PinName="Hit",Direction="EGPD_Output",'
        'PinType.PinCategory="struct",PinType.PinSubCategory="",PinType.PinSubCategoryObject="/Script/CoreUObject.ScriptStruct\'/Script/Fake.HitThing\'",'
        'PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=(),PinType.ContainerType=None,DefaultValue="a, b",'
        'PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,)',
        "   not a pin line",
    ]

    def test_parse_pins(self):
        pins = reflectparse.parse_pins(self.PINS)
        self.assertEqual([p["name"] for p in pins], ["self", "Items", "Hit"])
        self_pin, items, hit = pins
        self.assertEqual((self_pin["dir"], self_pin["hidden"], self_pin["category"]), ("in", True, "object"))
        self.assertEqual(self_pin["subCategoryObject"], "/Script/Fake.Widget")
        self.assertEqual(self_pin["objectMeta"], "/Script/CoreUObject.Class")
        self.assertEqual((items["dir"], items["category"], items["container"]), ("out", "wildcard", "Array"))
        self.assertIsNone(items["subCategoryObject"])
        self.assertIsNone(items["objectMeta"])
        self.assertEqual((hit["category"], hit["subCategoryObject"], hit["objectMeta"]), ("struct", "/Script/Fake.HitThing", "/Script/CoreUObject.ScriptStruct"))
        self.assertEqual(hit["default"], "a, b")
        self.assertFalse(hit["hidden"])

    def test_cast_pin(self):
        pins = [{"name": "execute", "dir": "in", "category": "exec"},
                {"name": "then", "dir": "out", "category": "exec"},
                {"name": "CastFailed", "dir": "out", "category": "exec"},
                {"name": "Object", "dir": "in", "category": "wildcard"},
                {"name": "AsFakeWidget", "dir": "out", "category": "object"},
                {"name": "bSuccess", "dir": "out", "category": "bool"}]
        self.assertEqual(reflectparse.cast_pin(pins), "AsFakeWidget")
        self.assertIsNone(reflectparse.cast_pin(pins[:3]))

    def test_bodies_and_skip(self):
        ct = "/Script/CoreUObject.Class'/Script/Fake.Widget'"
        self.assertEqual(reflectparse.call_body(ct, "DoIt"), ['FunctionReference=(MemberParent="%s",MemberName="DoIt")' % ct])
        self.assertEqual(reflectparse.get_body(ct, "Size"), ['VariableReference=(MemberParent="%s",MemberName="Size")' % ct])
        self.assertEqual(reflectparse.cast_body(ct), ['TargetType="%s"' % ct])
        self.assertEqual(reflectparse.break_body("/Script/Fake.HitThing"),
                         ["bMadeAfterOverridePinRemoval=True", 'StructType="/Script/CoreUObject.ScriptStruct\'/Script/Fake.HitThing\'"'])
        self.assertIn("UserDefinedStruct", reflectparse.break_body("/Game/S_X.S_X")[1])
        line = reflectparse.class_pin_line("/Game/BP_X.BP_X_C", "/Script/Engine.BlueprintGeneratedClass")
        self.assertIn("DefaultObject=\"/Script/Engine.BlueprintGeneratedClass'/Game/BP_X.BP_X_C'\"", line)
        parsed = reflectparse.parse_pins([line])
        self.assertEqual((parsed[0]["name"], parsed[0]["category"]), ("Class", "class"))
        self.assertTrue(reflectparse.SKIP_FUNCTION("ExecuteUbergraph_BP_X"))
        self.assertTrue(reflectparse.SKIP_FUNCTION("OnHit__DelegateSignature"))
        self.assertFalse(reflectparse.SKIP_FUNCTION("DoIt"))


class StdioOptInTests(unittest.TestCase):
    def tool_names(self, opt_in):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        env = dict(os.environ, ABF_HEADLESS_DIR=_TMP)
        env.pop("ABF_ABFC_TOOLS", None)
        if opt_in:
            env["ABF_ABFC_TOOLS"] = "1"
        p = subprocess.run([sys.executable, "-I", str(ROOT / "server.py")], input="\n".join(json.dumps(r) for r in reqs) + "\n",
                           capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
        replies = {json.loads(l)["id"]: json.loads(l) for l in p.stdout.splitlines() if l.strip()}
        return [t["name"] for t in replies[2]["result"]["tools"]]

    def test_tools_register_only_when_opted_in(self):
        plain = self.tool_names(False)
        extra = self.tool_names(True)
        for n in ("reflect", "abfc_extensions"):
            self.assertNotIn(n, plain)
            self.assertIn(n, extra)
        self.assertEqual(len(set(extra)), len(extra))
        self.assertEqual(len(extra), len(plain) + 2)


if __name__ == "__main__":
    unittest.main()
