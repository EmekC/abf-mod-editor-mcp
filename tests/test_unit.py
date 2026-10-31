"""Unit tests: no editor, no network, no real mod. Run with  python -m unittest tests.test_unit -v

Nothing here may reach server.run_json, server.execute or the file bridge: the editor side is a fake.
"""
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
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
    _TMP = tempfile.mkdtemp(prefix="abfunit")
    os.environ["ABF_HEADLESS_DIR"] = _TMP


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


import bpscan  # noqa: E402
import headless  # noqa: E402
import headless_tools  # noqa: E402
import paths  # noqa: E402
import staging  # noqa: E402
from bpkit import layout, specs, t3d  # noqa: E402
from bpkit.t3d import GraphModel, GraphNode, SpecError  # noqa: E402


# ---------------------------------------------------------------- t3d

class T3dTests(unittest.TestCase):
    def test_pin_line_attributes(self):
        p = t3d.pin_line("Amount", "int", "out", pin_id="ABC123", default="5")
        self.assertIn("PinId=ABC123,PinName=\"Amount\"", p)
        self.assertIn('Direction="EGPD_Output"', p)
        self.assertIn('PinType.PinCategory="int"', p)
        self.assertIn('DefaultValue="5"', p)
        self.assertTrue(p.endswith("bOrphanedPin=False,)"))
        arr = t3d.pin_line("L", "array:int")
        self.assertIn("PinType.ContainerType=Array", arr)
        obj = t3d.pin_line("O", "object:/Script/Engine.Actor")
        self.assertIn("PinType.PinSubCategoryObject=\"/Script/CoreUObject.Class'/Script/Engine.Actor'\"", obj)
        self.assertIn("Direction", t3d.pin_line("X", "exec", "out"))
        self.assertNotIn("Direction", t3d.pin_line("X", "exec"))

    def test_user_pin_and_var_line(self):
        self.assertEqual(t3d.user_pin("A", "int", "out"),
                         'CustomProperties UserDefinedPin (PinName="A",PinType=(PinCategory="int"),DesiredPinDirection=EGPD_Output)')
        v = t3d.var_line("Speed", "real", guid="G", default="2.5")
        self.assertIn('PinCategory="real",PinSubCategory="double"', v)
        self.assertIn('DefaultValue="2.5"', v)
        self.assertIn("ContainerType=Array", t3d.var_line("L", "array:int"))

    def test_type_grammar(self):
        self.assertEqual(t3d.parse_type("real")["sub"], "double")
        self.assertEqual(t3d.parse_type("double")["cat"], "real")
        self.assertEqual(t3d.parse_type("float")["sub"], "float")
        self.assertEqual(t3d.parse_type("array:bool")["container"], "Array")
        for kind in ("object", "class", "softobject", "softclass", "interface", "struct", "enum"):
            self.assertTrue(t3d.parse_type(kind + ":/Script/Engine.Actor")["obj"])
        self.assertEqual(t3d.parse_type("enum:/Script/Engine.EWhatever")["cat"], "byte")

    def test_type_refusals(self):
        for bad in ("struct:", "struct", "object:", "set:int", "map:int", "array:array:int", "banana", "", None):
            with self.assertRaises(SpecError, msg=repr(bad)):
                t3d.parse_type(bad)

    def test_two_pass_order(self):
        e = t3d.entry_node("F", [])
        r = t3d.result_node("F", [])
        text = t3d.graph_text([e.defn(), r.defn()])
        lines = text.split("\n")
        decl = [i for i, l in enumerate(lines) if "Begin Object Class=" in l]
        defi = [i for i, l in enumerate(lines) if l.strip().startswith('Begin Object Name=')]
        self.assertEqual(len(decl), 2)
        self.assertEqual(len(defi), 2)
        self.assertLess(max(decl), min(defi))
        self.assertIn('Nodes(0)="/Script/BlueprintGraph.K2Node_FunctionEntry\'K2Node_FunctionEntry_0\'"', text)
        self.assertIn("Schema=", text)

    def _pair(self):
        a = GraphNode.from_export("/Script/BlueprintGraph.K2Node_A", "A_0", [
            "NodePosX=10",
            t3d.pin_line("then", "exec", "out", pin_id="AAAA0001"),
            t3d.pin_line("Val", "int", "out", pin_id="AAAA0002")])
        b = GraphNode.from_export("/Script/BlueprintGraph.K2Node_B", "B_0", [
            "NodePosX=300",
            t3d.pin_line("execute", "exec", pin_id="BBBB0001"),
            t3d.pin_line("Count", "int", pin_id="BBBB0002", default="old \\\"q\\\" value")])
        m = GraphModel()
        m.add(a)
        m.add(b)
        return m, a, b

    def test_link_both_sided_and_unlink(self):
        m, a, b = self._pair()
        m.wire("A_0", "then", "B_0", "execute")
        self.assertEqual(a.pin("then").links(), [("B_0", "BBBB0001")])
        self.assertEqual(b.pin("execute").links(), [("A_0", "AAAA0001")])
        self.assertIn(",LinkedTo=(B_0 BBBB0001,),PersistentGuid=", a.pin("then").text)
        m.unwire("A_0", "then", "B_0", "execute")
        self.assertEqual(a.pin("then").links(), [])
        self.assertNotIn("LinkedTo", a.pin("then").text)
        self.assertEqual(b.pin("execute").links(), [])

    def test_wire_direction_and_pin_errors(self):
        m, a, b = self._pair()
        with self.assertRaises(SpecError) as cm:
            m.wire("B_0", "execute", "A_0", "then")
        self.assertIn("output", str(cm.exception))
        with self.assertRaises(SpecError) as cm:
            m.wire("A_0", "nope", "B_0", "execute")
        self.assertIn("Available", str(cm.exception))

    def test_escaped_quote_default(self):
        m, a, b = self._pair()
        self.assertIn('DefaultValue="old \\"q\\" value"', b.pin("Count").text)
        b.set_default("Count", "3")
        self.assertIn(',DefaultValue="3",PersistentGuid=', b.pin("Count").text)
        self.assertNotIn("old", b.pin("Count").text)
        b.set_default_object("Count", "/Script/Engine.Actor")
        self.assertIn(',DefaultObject="/Script/Engine.Actor",PersistentGuid=', b.pin("Count").text)

    def test_model_remove_drops_links(self):
        m, a, b = self._pair()
        m.wire("A_0", "then", "B_0", "execute")
        m.wire("A_0", "Val", "B_0", "Count")
        m.remove("B_0")
        self.assertNotIn("B_0", m.nodes)
        for p in a.pins:
            self.assertEqual(p.links(), [])

    def test_unique_name_skips_taken(self):
        m, a, b = self._pair()
        m.add(GraphNode("/Script/BlueprintGraph.K2Node_CallFunction", "K2Node_CallFunction_9001", [], []))
        self.assertEqual(m.unique_name("/Script/BlueprintGraph.K2Node_CallFunction"), "K2Node_CallFunction_9002")
        self.assertEqual(m.unique_name("/Script/BlueprintGraph.K2Node_Event"), "K2Node_Event_9001")

    def test_block_round_trip_and_nodes_restriction(self):
        e = t3d.entry_node("F", [{"name": "In", "type": "int"}])
        r = t3d.result_node("F", [{"name": "Out", "type": "int"}])
        m = GraphModel()
        m.add(e)
        m.add(r)
        m.wire("K2Node_FunctionEntry_0", "then", "K2Node_FunctionResult_0", "execute")
        block = t3d.graph_text(m.defs())
        # a node that lingers in the export (deleted) but is not in Nodes() must be ignored
        ghost = GraphNode("/Script/BlueprintGraph.K2Node_Event", "K2Node_Event_0", ["NodePosX=0"], [])
        block = block.replace('      Schema=', '      Begin Object Class=/Script/BlueprintGraph.K2Node_Event Name="K2Node_Event_0"\n      End Object\n      Schema=', 1)
        again = GraphModel.from_block(block)
        self.assertEqual(list(again.nodes), ["K2Node_FunctionEntry_0", "K2Node_FunctionResult_0"])
        self.assertEqual(again.nodes["K2Node_FunctionEntry_0"].pin("then").links()[0][0], "K2Node_FunctionResult_0")
        self.assertEqual(len(again.nodes["K2Node_FunctionEntry_0"].pins), 2)
        self.assertIn("DesiredPinDirection=EGPD_Output", "\n".join(again.nodes["K2Node_FunctionEntry_0"].head))
        del ghost

    def test_composite_refused(self):
        block = ('      Begin Object Class=/Script/BlueprintGraph.K2Node_Composite Name="K2Node_Composite_0"\n      End Object\n'
                 '      Begin Object Name="K2Node_Composite_0"\n         NodePosX=0\n      End Object\n'
                 "      Nodes(0)=\"/Script/BlueprintGraph.K2Node_Composite'K2Node_Composite_0'\"")
        with self.assertRaises(SpecError) as cm:
            GraphModel.from_block(block)
        self.assertIn("collapsed", str(cm.exception))

    def test_template_gets_new_ids_and_delegate_guid(self):
        guid = "0123456789ABCDEF0123456789ABCDEF"
        pin_out = t3d.pin_line("OutputDelegate", "exec", "out", pin_id="OLD1").replace(
            "PinSubCategoryMemberReference=()", 'PinSubCategoryMemberReference=(MemberName="X",MemberGuid=%s)' % guid)
        lines = ['CustomFunctionName="X"', "NodePosX=5", "NodeGuid=FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "ErrorMsg=\"bad\"", pin_out,
                 t3d.pin_line("then", "exec", "out", pin_id="OLD2") .replace(",PersistentGuid=", ",LinkedTo=(Z 1,),PersistentGuid=")]
        n = GraphNode.from_template("/Script/BlueprintGraph.K2Node_CustomEvent", "K2Node_CustomEvent_9001", lines, (40, 80))
        self.assertIn("NodeGuid=" + guid, n.head)
        self.assertEqual(n.pos(), (40, 80))
        self.assertNotIn("ErrorMsg=\"bad\"", n.head)
        self.assertEqual(sum(1 for h in n.head if h.startswith("NodePosX")), 1)
        for p in n.pins:
            self.assertNotIn("OLD", p.id)
            self.assertEqual(p.links(), [])
        n.move(-5, 9)
        self.assertEqual(n.pos(), (-5, 9))

    def test_retarget(self):
        cls = "/Script/Engine.BlueprintGeneratedClass'/Mod/Real.Real_C'"
        lines = ["FunctionReference=(MemberParent=\"/Script/Engine.BlueprintGeneratedClass'/Mod/_McpTmp/BP_BkScratch3.BP_BkScratch3_C'\")",
                 'x="/Mod/_McpTmp/BP_BkScratch3.BP_BkScratch3_C"', "unrelated"]
        out = t3d.retarget(lines, cls)
        self.assertEqual(out[0], 'FunctionReference=(MemberParent="%s")' % cls)
        self.assertEqual(out[1], 'x="/Mod/Real.Real_C"')
        self.assertEqual(out[2], "unrelated")

    def test_widget_tree_nested(self):
        root = {"class": "VerticalBox", "name": "Root", "children": [
            {"class": "TextBlock", "name": "Title", "text": "Hi", "slot": {"Padding": "(Left=7.000000)"}},
            {"class": "HorizontalBox", "name": "Row", "children": [{"class": "ProgressBar", "name": "Bar", "props": {"Percent": "0.5"}}]}]}
        text, rootname = t3d.widget_tree_text(root)
        self.assertEqual(rootname, "Root")
        self.assertIn('Begin Object Class=/Script/UMG.VerticalBoxSlot Name="Root_Slot0"', text)
        self.assertIn('Begin Object Class=/Script/UMG.HorizontalBoxSlot Name="Row_Slot0"', text)
        self.assertIn("Padding=(Left=7.000000)", text)
        self.assertIn('Slot="/Script/UMG.VerticalBoxSlot\'Root.Root_Slot0\'"', text)
        self.assertIn('Text=NSLOCTEXT("Mcp", "Title", "Hi")', text)
        self.assertIn("Percent=0.5", text)
        self.assertIn('RootWidget="/Script/UMG.VerticalBox\'Root\'"', text)
        self.assertEqual(text.count("bIsVariable=True"), 4)
        self.assertLess(text.rfind("Begin Object Class="), text.find('Begin Object Name='))

    def test_widget_tree_refusals(self):
        with self.assertRaises(SpecError):
            t3d.widget_tree_text({"class": "TextBlock", "name": "T", "children": [{"class": "TextBlock", "name": "U"}]})
        with self.assertRaises(SpecError):
            t3d.widget_tree_text({"class": "VerticalBox", "name": "A", "children": [{"class": "Image", "name": "A"}]})

    def test_timeline_and_animation_text(self):
        text, tn, guid = t3d.timeline_text({"name": "T1", "length": 2, "loop": True,
                                            "tracks": [{"name": "A", "keys": [[0, 0], [1, 5]]}, {"name": "B", "keys": [[0, 1]]}]})
        self.assertEqual(tn, "T1_Template")
        self.assertIn('Name="T1_A_Curve"', text)
        self.assertIn('Name="T1_B_Curve"', text)
        self.assertIn("FloatTracks(1)=", text)
        self.assertIn("bLoop=True", text)
        self.assertIn("TimelineGuid=" + guid, text)
        anim = t3d.animation_text({"name": "Fade", "tracks": [{"widget": "Txt", "property": "RenderOpacity", "keys": []}]},
                                  {"Txt": "/Script/UMG.TextBlock"})
        self.assertIn('Name="Fade"', anim)
        self.assertIn("bIsRootWidget=False", anim)
        with self.assertRaises(SpecError):
            t3d.animation_text({"name": "F", "tracks": [{"widget": "Nope"}]}, {"Txt": "x"})

    def test_function_nodes(self):
        e = t3d.entry_node("F", [{"name": "A", "type": "int"}], flags=t3d.FLAGS_FN | t3d.FLAGS_PURE, exec_pin=False)
        self.assertEqual([p.name for p in e.pins], ["A"])
        self.assertIn("ExtraFlags=%d" % (201457664 | 0x10000000), e.head)
        iface = t3d.entry_node("P", [{"name": "In", "type": "int"}], parent="/Script/Engine.BlueprintGeneratedClass'/M/I.I_C'", editable=False)
        self.assertFalse(any("UserDefinedPin" in h for h in iface.head))
        self.assertNotIn("bIsEditable=True", iface.head)
        a, b = t3d.tunnel_nodes([{"name": "Exec", "type": "exec"}], [{"name": "Then", "type": "exec"}])
        self.assertIn("bCanHaveOutputs=True", a.head)
        self.assertIn("bCanHaveInputs=True", b.head)
        mi = t3d.macro_instance_node("K2Node_MacroInstance_0", "/M/B.B", "GUID", [{"name": "Exec", "type": "exec"}], [{"name": "Then", "type": "exec"}], (0, 0))
        self.assertIn("MacroGraphReference=(GraphBlueprint=\"/Script/Engine.Blueprint'/M/B.B'\",GraphGuid=GUID)", mi.head)
        self.assertEqual(t3d.self_node("S", (0, 0)).pin("self").direction, "out")


# ---------------------------------------------------------------- layout

class LayoutTests(unittest.TestCase):
    OCC = [(0, 0, 320, 86), (0, 200, 320, 86), (0, 400, 320, 86)]       # the three default event bubbles

    def _case(self, occupied):
        new = [("a", 320, 112), ("b", 320, 86), ("c", 320, 164), ("d", 320, 86), ("e", 320, 86)]
        wires = [("a", "b"), ("b", "c"), ("a", "d"), ("d", "c"), ("c", "a")]       # includes a cycle
        return layout.place(new, wires, occupied), new

    def test_no_overlaps(self):
        pos, new = self._case(self.OCC)
        rects = {i: (pos[i][0], pos[i][1], w, h) for i, w, h in new}
        for i, r in rects.items():
            for o in self.OCC:
                self.assertFalse(layout.overlaps(r, o), (i, o))
            for j, s in rects.items():
                if i != j:
                    self.assertFalse(layout.overlaps(r, s), (i, j))

    def test_deterministic(self):
        self.assertEqual(self._case(self.OCC)[0], self._case(self.OCC)[0])

    def test_empty_starts_at_origin(self):
        pos, _ = self._case([])
        self.assertEqual(min(p[0] for p in pos.values()), 0)
        self.assertEqual(min(p[1] for p in pos.values()), 0)

    def test_columns_follow_wires(self):
        pos = layout.place([("a", 320, 100), ("b", 320, 100), ("c", 320, 100)], [("a", "b"), ("b", "c")], [])
        self.assertLess(pos["a"][0], pos["b"][0])
        self.assertLess(pos["b"][0], pos["c"][0])

    def test_size(self):
        self.assertEqual(layout.node_size(2, 5), (320, 190))


# ---------------------------------------------------------------- specs

class SpecTests(unittest.TestCase):
    GOOD = {"assets": [{
        "asset": "/Mod/Folder/BP_X", "create": {"class": "Blueprint", "parent": "/Script/Engine.Actor", "if_missing": True},
        "variables": [{"name": "Score", "type": "int", "default": "10"}],
        "functions": [{"name": "AddScore", "inputs": [{"name": "Amount", "type": "int"}], "outputs": [{"name": "Total", "type": "int"}],
                       "nodes": [{"id": "g", "k": "get", "name": "Score"}, {"id": "s", "k": "set", "name": "Score"}],
                       "wires": [["entry.then", "s.execute"], ["g.Score", "s.Score"]]}],
        "graphs": [{"graph": "EventGraph", "remove": ["K2Node_A_0"], "unwire": [["K2Node_A_0.then", "K2Node_B_0.execute"]],
                    "add": [{"id": "n", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Add_IntInt"}],
                    "wires": [["n.ReturnValue", "K2Node_B_0.Count"]]}],
    }]}

    def problems(self, mutate):
        batch = json.loads(json.dumps(self.GOOD))
        mutate(batch["assets"][0])
        return specs.validate(batch, "Mod")

    def test_valid_sample_passes(self):
        self.assertEqual(specs.validate(self.GOOD, "Mod"), [])

    def test_game_refused(self):
        self.assertTrue(any("/Game" in p for p in self.problems(lambda a: a.update(asset="/Game/Maps/X"))))

    def test_other_root_refused(self):
        self.assertTrue(any("must start with /Mod/" in p for p in self.problems(lambda a: a.update(asset="/Other/BP_X"))))

    def test_reparent_refused(self):
        self.assertTrue(any("reparent" in p for p in self.problems(lambda a: a["create"].update(reparent="/Script/Engine.Pawn"))))

    def test_dotted_id_refused(self):
        def m(a):
            a["functions"][0]["nodes"][0]["id"] = "g.x"
        self.assertTrue(any("'.'" in p for p in self.problems(m)))

    def test_unknown_kind_refused(self):
        def m(a):
            a["functions"][0]["nodes"][0]["k"] = "teleport"
        self.assertTrue(any("unknown kind" in p for p in self.problems(m)))

    def test_bad_wire_and_type(self):
        self.assertTrue(any("unknown node id" in p for p in self.problems(lambda a: a["functions"][0]["wires"].append(["zz.then", "s.execute"]))))
        self.assertTrue(any("not \"id.pin\"" in p for p in self.problems(lambda a: a["functions"][0]["wires"].append(["zz", "s.execute"]))))
        self.assertTrue(any("struct" in p for p in self.problems(lambda a: a["variables"].append({"name": "V", "type": "struct:"}))))
        self.assertTrue(any("set types" in p or "set" in p for p in self.problems(lambda a: a["variables"].append({"name": "V", "type": "set:int"}))))

    def test_create_class_and_missing_keys(self):
        self.assertTrue(any("create.class" in p for p in self.problems(lambda a: a["create"].update({"class": "Material"}))))
        self.assertTrue(any("needs 'function'" in p
                            for p in self.problems(lambda a: a["graphs"][0]["add"].append({"id": "q", "k": "call"}))))

    def test_raw_python_guard(self):
        self.assertTrue(any("delete_asset" in p for p in self.problems(lambda a: a.update(raw=[{"kind": "python", "code": "unreal.EditorAssetLibrary.delete_asset('x')"}]))))

    def test_node_kinds_text(self):
        class Env:
            def class_text(self, p):
                return t3d.class_ref(p)

            def self_class_text(self):
                return "SELF"

            def timeline_guid(self, n):
                return "G"
        env = Env()
        cls, body = specs.NODE_KINDS["call"]({"id": "x", "k": "call", "function": "F"}, env)
        self.assertEqual(body, ['FunctionReference=(MemberParent="SELF",MemberName="F")'])
        cls, body = specs.NODE_KINDS["get"]({"id": "x", "k": "get", "name": "V"}, env)
        self.assertTrue(cls.endswith("K2Node_VariableGet"))
        self.assertEqual(body, ['VariableReference=(MemberName="V",bSelfContext=True)'])
        cls, body = specs.NODE_KINDS["override"]({"id": "x", "k": "override", "function": "ReceiveBeginPlay", "class": "/Script/Engine.Actor"}, env)
        self.assertIn("bOverrideFunction=True", body)
        cls, body = specs.NODE_KINDS["event"]({"id": "x", "k": "event", "name": "Go", "params": [{"name": "A", "type": "int"}]}, env)
        self.assertEqual(body[0], 'CustomFunctionName="Go"')
        self.assertIn("UserDefinedPin", body[1])
        self.assertEqual(specs.NODE_KINDS["self"]({}, env), (None, None))
        self.assertEqual(sorted(specs.NODE_KINDS), sorted(["call", "get", "set", "event", "override", "branch", "sequence", "cast", "spawn",
                                                           "create_widget", "call_dispatcher", "bind_dispatcher", "timeline", "message",
                                                           "self", "macro", "node"]))


# ---------------------------------------------------------------- headless

class HeadlessTests(unittest.TestCase):
    LOG = "\n".join([
        "[2026.10.30-19.46.43:164][  0]LogBlueprint: Error: [Compiler] This blueprint (self) is not a BP_BkScratch1_C, therefore ' Target ' must have a connection. from Source: /TestModXyz34/_Lab/BP_LabC3.BP_LabC3",
        "[2026.10.30-19.46.43:231][  0]LogInit: Display: LogBlueprint: Error: [Compiler] This blueprint (self) is not a BP_BkScratch1_C, therefore ' Target ' must have a connection. from Source: /TestModXyz34/_Lab/BP_LabC3.BP_LabC3",
        r"[2026.10.30-20.30.56:159][ 56]LogBlueprint: Error: [AssetLog] C:\Users\Me\bp\mods2\TestModXyz34\Content\_Lab\BP_LabC2.uasset: [Compiler] In use pin  Lab Box  no longer exists on node  Get .",
        "LogBlueprint: Warning: [AssetLog] /Mod/X.X: [Compiler] Something is odd",
        "LogTemp: Display: nothing to see",
    ])

    def test_compile_parse_and_dedupe(self):
        r = headless.parse_compile_log(self.LOG)
        self.assertEqual(len(r["errors"]), 2)
        self.assertEqual(r["errors"][0]["asset"], "/TestModXyz34/_Lab/BP_LabC3")
        self.assertIn("is not a BP_BkScratch1_C", r["errors"][0]["message"])
        self.assertEqual(len(r["warnings"]), 1)
        self.assertEqual(r["warnings"][0], {"asset": "/Mod/X", "message": "Something is odd"})

    def test_windows_path_maps_to_asset(self):
        r = headless.parse_compile_log(self.LOG)
        self.assertEqual(r["errors"][1]["asset"], "/TestModXyz34/_Lab/BP_LabC2")
        self.assertEqual(headless.parse_compile_log(self.LOG, "Other")["errors"][1]["asset"], "/Other/_Lab/BP_LabC2")

    def test_audit_parse(self):
        done = ("[x][56]LogAbioticAuditModAssets: Auditing /TestModXyz34/Example/WBP_Example.WBP_Example (WidgetBlueprint).\n"
                "[x][56]LogAbioticAuditModAssets: Auditing /TestModXyz34/AbioticModConfig.AbioticModConfig (AbioticModConfiguration).\n"
                "[x][56]AssetAudit: Asset audit complete: 2 Error(s), 3 Warning(s), took 4,200.1ms to complete.\n")
        r = headless.parse_audit_log(done)
        self.assertTrue(r["completed"])
        self.assertEqual((r["errors"], r["warnings"]), (2, 3))
        self.assertEqual(r["audited"], ["/TestModXyz34/Example/WBP_Example", "/TestModXyz34/AbioticModConfig"])
        part = headless.parse_audit_log(done.split("AssetAudit")[0])
        self.assertFalse(part["completed"])
        self.assertEqual(part["errors"], 0)

    def test_runtime_parse(self):
        text = ("[1][0]LogScript: Error: Script Msg: Accessed None\n[1][0]LogTemp: Display: ok\n"
                "[2][0]LogScript: Warning: Something\nBlueprint Runtime Error: \"Accessed None\"\n"
                "[1][0]LogScript: Error: Script Msg: Accessed None\n")
        r = headless.parse_runtime_log(text)
        self.assertEqual(len(r), 3)

    def test_scrub(self):
        self.assertNotIn("Bearer abc123", headless.scrub("GET /x Authorization: Bearer abc123 now").replace("Authorization=***", ""))
        out =headless.scrub("token=abc123 and api_key: zzz and Secret=hunter2")
        for leaked in ("abc123", "zzz", "hunter2"):
            self.assertNotIn(leaked, out)
        self.assertEqual(headless.scrub("plain line"), "plain line")

    def test_runlock_stale_pid_is_replaced(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        lock = headless.root() / "run.lock"
        lock.write_text(str(p.pid))
        with headless.RunLock():
            self.assertEqual(int(lock.read_text()), os.getpid())
        self.assertFalse(lock.exists())

    def test_runlock_busy(self):
        with headless.RunLock():
            with self.assertRaises(headless.HeadlessError):
                with headless.RunLock():
                    pass
        self.assertFalse((headless.root() / "run.lock").exists())

    def test_space_in_root_refused(self):
        with mock.patch.dict(os.environ, {"ABF_HEADLESS_DIR": os.path.join(_TMP, "with space")}):
            with self.assertRaises(headless.HeadlessError):
                headless.root()
            with self.assertRaises(staging.StagingError):
                staging.root()

    def test_new_run_copies_job_and_prunes(self):
        for _ in range(headless.KEEP_RUNS + 3):
            run = headless.new_run("t")
        self.assertTrue((run / "abf_job.py").is_file())
        self.assertLessEqual(len(list((headless.root() / "runs").iterdir())), headless.KEEP_RUNS)

    def test_child_env(self):
        run = headless.new_run("t")
        env = headless.child_env(run, run / "job.json")
        self.assertEqual(env["ABF_MCP_HIDDEN"], "1")
        self.assertTrue(env["LOCALAPPDATA"].startswith(str(headless.root())))
        self.assertNotIn("\\", env["ABF_MCP_JOB"])
        self.assertTrue(env["ABF_MCP_KIT"].endswith("/editor"))


# ---------------------------------------------------------------- staging

class FakeEditor:
    def __init__(self, state="closed", refused=None, real=None, missing=None, on_prepare=None, prepare_raises=None, detail=""):
        self.st, self.refused, self.calls, self.real, self.missing = state, refused, [], real, missing
        self.on_prepare, self.prepare_raises, self.detail = on_prepare, prepare_raises, detail
        self.seen = {}

    def state(self, mod):
        self.calls.append("state")
        return self.st

    def prepare(self, packages):
        self.calls.append("prepare")
        if self.prepare_raises:
            raise self.prepare_raises
        if self.on_prepare:
            self.on_prepare()
        if self.real:
            self.seen["at_prepare"] = (self.real / "Content" / "A.uasset").read_bytes()
        if self.refused:
            return {"refused": self.refused, "reason": "modified assets"}
        return {"refused": []}

    def reload(self, packages):
        self.calls.append("reload")
        if self.real:
            self.seen["at_reload"] = (self.real / "Content" / "A.uasset").read_bytes()
        return {"missing": self.missing or [], "loaded": packages}


def make_fake_mod(base):
    mod = Path(base) / "Fake"
    (mod / "Content" / "Sub").mkdir(parents=True)
    (mod / "Saved").mkdir()
    (mod / "Saved" / "junk.log").write_text("x")
    (mod / "Fake.uplugin").write_text(json.dumps({"FileVersion": 3, "FriendlyName": "Fake", "CanContainContent": True,
                                                   "ExplicitlyLoaded": True}), encoding="utf-8-sig")
    (mod / "Fake.abfmod").write_text('{"ModId":0}')
    (mod / "Content" / "A.uasset").write_bytes(b"A-original" * 50)
    (mod / "Content" / "Sub" / "B.uasset").write_bytes(b"B-original" * 50)
    return mod


def tree_bytes(p):
    return {f.relative_to(p).as_posix(): f.read_bytes() for f in Path(p).rglob("*") if f.is_file()}


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="abffake"))
        self.real = make_fake_mod(self.work)
        shutil.rmtree(staging.root() / "stages", ignore_errors=True)
        self.stage = staging.sync(self.real)
        self.gate_ok = lambda packages: []

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def stage_write(self, rel, data):
        p = self.stage.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def test_sync_excludes_and_records(self):
        self.assertFalse((self.stage.dir / "Saved").exists())
        self.assertTrue((self.stage.dir / "Content" / "A.uasset").is_file())
        meta = self.stage.meta()
        self.assertEqual(sorted(meta["baseline"]), ["Content/A.uasset", "Content/Sub/B.uasset"])
        self.assertEqual(meta["baseline"]["Content/A.uasset"]["sha256"], hashlib.sha256(b"A-original" * 50).hexdigest())
        self.assertEqual(self.stage.file_count, 4)

    def test_sync_refuses_pending_changes_and_kit(self):
        self.stage_write("Content/A.uasset", b"edited")
        with self.assertRaises(staging.StagingError) as cm:
            staging.sync(self.real)
        self.assertIn("unpublished", str(cm.exception))
        staging.sync(self.real, discard_changes=True)
        self.assertEqual((self.stage.dir / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)
        with self.assertRaises(staging.StagingError):
            staging.sync(self.real / "Content", forbidden=[self.work])
        two = self.work / "Two"
        two.mkdir()
        with self.assertRaises(staging.StagingError):
            staging.sync(two)

    def test_set_uplugin_modes(self):
        staging.set_uplugin(self.stage, "picker")
        d = json.loads(self.stage.uplugin_path.read_text(encoding="utf-8"))
        self.assertEqual(d["FriendlyName"], "MCPSTAGEFake")
        self.assertTrue(d["ExplicitlyLoaded"])
        staging.set_uplugin(self.stage, "mount")
        d = json.loads(self.stage.uplugin_path.read_text(encoding="utf-8"))
        self.assertFalse(d["ExplicitlyLoaded"])
        self.assertTrue(d["EnabledByDefault"])
        self.assertEqual(d["FriendlyName"], "MCPSTAGEFake")

    def test_diff_and_tooling(self):
        self.stage_write("Content/A.uasset", b"edited")
        self.stage_write("Content/New.uasset", b"new")
        self.stage_write("Content/_McpTest/H.uasset", b"harness")
        self.stage_write("Content/_McpTmp/T.uasset", b"tmp")
        (self.stage.dir / "Content" / "Sub" / "B.uasset").unlink()
        d = staging.diff(self.stage)
        self.assertEqual(d["changed"], ["Content/A.uasset"])
        self.assertEqual(d["added"], ["Content/New.uasset"])
        self.assertEqual(d["removed"], ["Content/Sub/B.uasset"])
        self.assertEqual(d["conflicts"], [])
        staging.strip_tooling(self.stage)
        self.assertFalse((self.stage.dir / "Content" / "_McpTest").exists())

    def test_conflict_refuses(self):
        self.stage_write("Content/A.uasset", b"staged edit")
        (self.real / "Content" / "A.uasset").write_bytes(b"someone else")
        before = tree_bytes(self.real)
        ed = FakeEditor("closed")
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "conflicts")
        self.assertEqual(tree_bytes(self.real), before)
        self.assertEqual(ed.calls, [])

    def test_added_file_already_in_real_conflicts(self):
        self.stage_write("Content/Sub/C.uasset", b"mine")
        (self.real / "Content" / "Sub" / "C.uasset").write_bytes(b"theirs")
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertEqual(r["refused"], "conflicts")

    def test_compile_gate_refuses(self):
        self.stage_write("Content/A.uasset", b"edited")
        before = tree_bytes(self.real)
        r = staging.publish(self.stage, FakeEditor("closed"), lambda pk: [{"asset": "/Fake/A", "message": "boom"}])
        self.assertEqual(r["refused"], "compile errors")
        self.assertEqual(tree_bytes(self.real), before)

    def test_dirty_refusal_leaves_real_files_identical(self):
        self.stage_write("Content/A.uasset", b"edited")
        before = tree_bytes(self.real)
        backups_before = sorted((staging.root() / "backups" / "Fake").glob("*")) if (staging.root() / "backups" / "Fake").exists() else []
        ed = FakeEditor("open", refused=["/Fake/A"])
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "editor refused")
        self.assertEqual(tree_bytes(self.real), before)
        self.assertEqual(ed.calls, ["state", "prepare"])
        backups_after = sorted((staging.root() / "backups" / "Fake").glob("*")) if (staging.root() / "backups" / "Fake").exists() else []
        self.assertEqual(backups_before, backups_after)

    def test_closed_editor_copy_with_backup(self):
        self.stage_write("Content/A.uasset", b"edited")
        self.stage_write("Content/Sub/New.uasset", b"brand new")
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok, now="20260101-000000")
        self.assertEqual(r["route"], "file-copy")
        self.assertEqual(sorted(r["published"]), ["Content/A.uasset", "Content/Sub/New.uasset"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"edited")
        self.assertEqual((self.real / "Content" / "Sub" / "New.uasset").read_bytes(), b"brand new")
        backup = Path(r["backup"])
        self.assertFalse(staging._is_under(backup, self.real))
        self.assertEqual((backup / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)
        man = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["files"][0]["sha256"], hashlib.sha256(b"A-original" * 50).hexdigest())
        self.assertEqual(man["added"], ["Content/Sub/New.uasset"])
        self.assertFalse(list(self.real.rglob("*.mcpnew")))
        self.assertIn("Backup", r["message"])

    def test_live_order_prepare_copy_reload(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open", real=self.real)
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(ed.calls, ["state", "prepare", "reload"])
        self.assertEqual(ed.seen["at_prepare"], b"A-original" * 50)
        self.assertEqual(ed.seen["at_reload"], b"edited")
        self.assertEqual(r["route"], "live-reload")

    def test_reload_failure_restores(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open", real=self.real, missing=["/Fake/A"])
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "reload failed")
        self.assertTrue(r["restored"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)
        self.assertEqual(ed.calls, ["state", "prepare", "reload", "reload"])

    @unittest.skipUnless(sys.platform == "win32", "needs Windows share modes")
    def test_lock_probe_refuses(self):
        self.stage_write("Content/A.uasset", b"edited")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.CreateFileW(str(self.real / "Content" / "A.uasset"), 0xC0000000, 0, None, 3, 0x80, None)
        self.assertNotEqual(h, ctypes.c_void_p(-1).value)
        try:
            r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        finally:
            k.CloseHandle(h)
        self.assertEqual(r["refused"], "file locked")
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_copy_failure_restores(self):
        self.stage_write("Content/A.uasset", b"edited A")
        self.stage_write("Content/Sub/B.uasset", b"edited B")
        real_copy = shutil.copyfile

        def flaky(src, dst, *a, **k):
            if str(dst).endswith("B.uasset.mcpnew"):
                raise OSError("disk full (simulated)")
            return real_copy(src, dst, *a, **k)

        before = tree_bytes(self.real)
        with mock.patch.object(staging.shutil, "copyfile", side_effect=flaky):
            r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertEqual(r["refused"], "copy failed")
        self.assertTrue(r["restored"])
        self.assertEqual(tree_bytes(self.real), before)

    def test_baseline_updated_and_second_publish_noop(self):
        self.stage_write("Content/A.uasset", b"edited")
        staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        d = staging.diff(self.stage)
        self.assertEqual((d["changed"], d["added"], d["conflicts"]), ([], [], []))
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertEqual(r["published"], [])
        self.assertIsNone(r["route"])

    def test_removed_reported_not_deleted(self):
        (self.stage.dir / "Content" / "Sub" / "B.uasset").unlink()
        self.stage_write("Content/A.uasset", b"edited")
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertEqual(r["removed_in_staging"], ["Content/Sub/B.uasset"])
        self.assertTrue((self.real / "Content" / "Sub" / "B.uasset").is_file())
        self.assertIn("yourself", r["message"])

    def test_assets_filter_and_dry_run(self):
        self.stage_write("Content/A.uasset", b"edited")
        self.stage_write("Content/Sub/B.uasset", b"edited B")
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok, assets=["/Fake/Sub/B"], dry_run=True)
        self.assertEqual(r["would_publish"], ["Content/Sub/B.uasset"])
        self.assertEqual((self.real / "Content" / "Sub" / "B.uasset").read_bytes(), b"B-original" * 50)
        r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok, assets=["/Fake/Sub/B"])
        self.assertEqual(r["published"], ["Content/Sub/B.uasset"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_unreachable_refuses_before_anything(self):
        self.stage_write("Content/A.uasset", b"edited")
        before = tree_bytes(self.real)
        backups = staging.root() / "backups" / "Fake"
        n_before = len(list(backups.glob("*"))) if backups.exists() else 0
        ed = FakeEditor("unreachable", detail="Editor process 123 did not answer (TimeoutError).")
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "editor unreachable")
        self.assertIn("not answering", r["message"])
        self.assertIn("123", r["message"])
        self.assertEqual(ed.calls, ["state"])
        self.assertEqual(tree_bytes(self.real), before)
        self.assertEqual(len(list(backups.glob("*"))) if backups.exists() else 0, n_before)

    def test_edit_during_compile_gate_refuses_in_backup_loop(self):
        self.stage_write("Content/A.uasset", b"edited")

        def gate(packages):
            (self.real / "Content" / "A.uasset").write_bytes(b"user saved this meanwhile")
            return []
        r = staging.publish(self.stage, FakeEditor("closed"), gate)
        self.assertEqual(r["refused"], "conflicts")
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"user saved this meanwhile")
        self.assertFalse(list(self.real.rglob("*.mcpnew")))

    def test_added_file_appearing_during_gate_refuses(self):
        self.stage_write("Content/Sub/C.uasset", b"mine")

        def gate(packages):
            (self.real / "Content" / "Sub" / "C.uasset").write_bytes(b"theirs")
            return []
        r = staging.publish(self.stage, FakeEditor("closed"), gate)
        self.assertEqual(r["refused"], "conflicts")
        self.assertEqual((self.real / "Content" / "Sub" / "C.uasset").read_bytes(), b"theirs")

    def test_edit_during_prepare_refuses_and_reloads(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open", on_prepare=lambda: (self.real / "Content" / "A.uasset").write_bytes(b"saved during prepare"))
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "conflicts")
        self.assertEqual(ed.calls, ["state", "prepare", "reload"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"saved during prepare")

    def test_prepare_raising_reloads(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open", prepare_raises=TimeoutError("30 s"))
        r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "editor error")
        self.assertEqual(ed.calls, ["state", "prepare", "reload"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_backup_failure_after_prepare_reloads(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open")
        with mock.patch.object(staging.shutil, "copy2", side_effect=OSError("disk full (simulated)")):
            r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "backup failed")
        self.assertEqual(ed.calls, ["state", "prepare", "reload"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_failed_restore_is_reported_honestly(self):
        self.stage_write("Content/A.uasset", b"edited A")
        self.stage_write("Content/Sub/B.uasset", b"edited B")
        real_copy = shutil.copyfile

        def flaky(src, dst, *a, **k):
            if str(dst).endswith("B.uasset.mcpnew"):
                Path(dst).write_bytes(b"partial")
                raise OSError("disk full (simulated)")
            if not str(dst).endswith(".mcpnew"):
                raise PermissionError("locked during rollback (simulated)")
            return real_copy(src, dst, *a, **k)

        with mock.patch.object(staging.shutil, "copyfile", side_effect=flaky):
            r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertEqual(r["refused"], "copy failed")
        self.assertIs(r["restored"], False)
        self.assertIn("Content/A.uasset", r["failed_paths"])
        self.assertIn("FAILED", r["message"])
        self.assertFalse(list(self.real.rglob("*.mcpnew")))

    def test_partial_copy_leaves_no_mcpnew(self):
        self.stage_write("Content/A.uasset", b"edited A")
        real_copy = shutil.copyfile

        def flaky(src, dst, *a, **k):
            if str(dst).endswith(".mcpnew"):
                Path(dst).write_bytes(b"partial")
                raise OSError("disk full (simulated)")
            return real_copy(src, dst, *a, **k)

        with mock.patch.object(staging.shutil, "copyfile", side_effect=flaky):
            r = staging.publish(self.stage, FakeEditor("closed"), self.gate_ok)
        self.assertIs(r["restored"], True)
        self.assertFalse(list(self.real.rglob("*.mcpnew")))
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_manifest_write_failure_reloads(self):
        self.stage_write("Content/A.uasset", b"edited")
        ed = FakeEditor("open")
        real_write = Path.write_text

        def flaky(self_, *a, **k):
            if self_.name == "manifest.json":
                raise OSError("disk full (simulated)")
            return real_write(self_, *a, **k)

        with mock.patch.object(Path, "write_text", flaky):
            r = staging.publish(self.stage, ed, self.gate_ok)
        self.assertEqual(r["refused"], "backup failed")
        self.assertEqual(ed.calls, ["state", "prepare", "reload"])
        self.assertEqual((self.real / "Content" / "A.uasset").read_bytes(), b"A-original" * 50)

    def test_load_unknown_mod(self):
        with self.assertRaises(staging.StagingError) as cm:
            staging.load("NoSuchMod")
        self.assertIn("staging_sync", str(cm.exception))


# ---------------------------------------------------------------- headless_tools helpers

class LiveEditorTests(unittest.TestCase):
    def make(self, procs, run_json):
        return headless_tools.LiveEditor(run_json, (RuntimeError,), "STATUS", "Fake", None, procs)

    def test_no_process_is_closed_and_nothing_is_called(self):
        called = []
        ed = self.make(lambda: [], lambda *a, **k: called.append(1))
        self.assertEqual(ed.state("Fake"), "closed")
        self.assertEqual(called, [])

    def test_listing_failure_is_unreachable(self):
        def boom():
            raise headless.HeadlessError("powershell exploded")
        ed = self.make(boom, lambda *a, **k: {})
        self.assertEqual(ed.state("Fake"), "unreachable")
        self.assertIn("powershell exploded", ed.detail)

    @staticmethod
    def reply(pid=1, mods=(), mounted=False, dir_exists=False):
        return {"pid": pid, "open_mods": [{"root": m, "dir": "x"} for m in mods], "mounted": mounted, "dir_exists": dir_exists}

    def test_process_without_answer_is_unreachable(self):
        procs = lambda: [{"pid": 4242, "path": "x"}]   # noqa: E731
        for exc in (RuntimeError("remote"), TimeoutError("30 s"), ValueError("bad json")):
            def rj(*a, _e=exc, **k):
                raise _e
            ed = self.make(procs, rj)
            self.assertEqual(ed.state("Fake"), "unreachable", repr(exc))
            self.assertIn("4242", ed.detail)
        for reply in ({}, {"open_mods": "nope"}, None, {"pid": 4242, "open_mods": []}):
            ed = self.make(procs, lambda *a, _r=reply, **k: _r)
            self.assertEqual(ed.state("Fake"), "unreachable", repr(reply))

    def test_two_editors_are_unreachable_without_asking(self):
        called = []
        ed = self.make(lambda: [{"pid": 1, "path": "a"}, {"pid": 2, "path": "b"}], lambda *a, **k: called.append(1) or self.reply(1))
        self.assertEqual(ed.state("Fake"), "unreachable")
        self.assertIn("More than one", ed.detail)
        self.assertEqual(called, [])

    def test_pid_mismatch_is_unreachable(self):
        ed = self.make(lambda: [{"pid": 100, "path": "a"}], lambda *a, **k: self.reply(pid=999, mods=["Fake"]))
        self.assertEqual(ed.state("Fake"), "unreachable")
        self.assertIn("did not come from", ed.detail)
        ed = self.make(lambda: [{"pid": 100, "path": "a"}], lambda *a, **k: {"open_mods": [], "mounted": False, "dir_exists": False})
        self.assertEqual(ed.state("Fake"), "unreachable")

    def test_reachable_open_or_other(self):
        procs = lambda: [{"pid": 1, "path": None}]   # noqa: E731
        ed = self.make(procs, lambda *a, **k: self.reply(1, mods=["/fake"]))
        self.assertEqual(ed.state("Fake"), "open")
        ed = self.make(procs, lambda *a, **k: self.reply(1, mods=["/Other"]))
        self.assertEqual(ed.state("Fake"), "other")
        ed = self.make(procs, lambda *a, **k: self.reply(1))
        self.assertEqual(ed.state("Fake"), "other")

    def test_mounted_without_abioticmodconfig_is_open(self):
        procs = lambda: [{"pid": 1, "path": None}]   # noqa: E731
        ed = self.make(procs, lambda *a, **k: self.reply(1, mods=[], mounted=True))
        self.assertEqual(ed.state("Fake"), "open")
        ed = self.make(procs, lambda *a, **k: self.reply(1, mods=[], dir_exists=True))
        self.assertEqual(ed.state("Fake"), "open")

    def test_status_code_adds_pid_and_mount_checks_and_keeps_py_status(self):
        seen = {}

        def rj(code, args=None, timeout=None):
            seen["code"], seen["args"] = code, args
            return self.reply(1)
        ed = self.make(lambda: [{"pid": 1, "path": None}], rj)
        ed.state("Fake")
        self.assertTrue(seen["code"].startswith("STATUS"))
        self.assertIn("os.getpid()", seen["code"])
        self.assertIn("is_plugin_mounted", seen["code"])
        self.assertIn("does_directory_exist", seen["code"])
        self.assertEqual(seen["args"], {"mod": "Fake"})

    def test_command_line_never_leaves_the_listing(self):
        text = open(ROOT / "headless_tools.py", encoding="utf-8").read()
        self.assertIn('return [{"pid": r["pid"], "path": r["path"]} for r in kept]', text)
        ed = self.make(lambda: [{"pid": 7, "path": "p"}], lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
        ed.state("Fake")
        self.assertNotIn("command", ed.detail.lower())

    def test_filter_editor_processes(self):
        exe = "C:\\Kit\\AbioticFactor\\Binaries\\Win64\\Editor.exe"
        root = "C:\\Users\\Me\\AppData\\Local\\abf\\headless"
        rows = [
            {"pid": 1, "path": exe, "command_line": "x -Installed"},                                                  # the user's editor: kept
            {"pid": 2, "path": exe, "command_line": "x -userdir=C:/Users/Me/AppData/Local/abf/headless/ud -nosplash"},  # ours: dropped
            {"pid": 3, "path": "D:\\Other\\Editor.exe", "command_line": "whatever"},                                 # other kit: dropped
            {"pid": 4, "path": None, "command_line": "unknown"},                                                      # unreadable path: kept
            {"pid": 5, "path": exe, "command_line": None},                                                            # unreadable cmdline: kept
        ]
        kept = [r["pid"] for r in headless_tools.filter_editor_processes(rows, exe, root)]
        self.assertEqual(kept, [1, 4, 5])

    def test_valid_map(self):
        self.assertTrue(headless_tools.valid_map("Fake", "/Game/Maps/Empty"))
        self.assertTrue(headless_tools.valid_map("Fake", "/Fake/Maps/Test_1"))
        for bad in ("-userdir=C:/x", "/Game/Maps/Empty -plugin=x", "-plugin=/Game/x", "/Other/Maps/X", "/Game/../x", "", None, "/Game/"):
            self.assertFalse(headless_tools.valid_map("Fake", bad), repr(bad))

    def test_unchecked_assets_fail_closed(self):
        res = {"compiled": ["/Fake/A"], "skipped": [{"asset": "/Fake/D", "class": "DataAsset", "reason": "not a Blueprint"},
                                                     {"asset": "/Fake/X", "class": None, "reason": "no registry entry"},
                                                     {"asset": "/Fake/Y", "class": "Blueprint", "reason": "load returned None"}]}
        self.assertEqual([u["asset"] for u in headless_tools.unchecked(res)], ["/Fake/X", "/Fake/Y"])
        self.assertEqual(headless_tools.unchecked(None), [])


@unittest.skipUnless(sys.platform == "win32", "Windows job objects")
class JobObjectTests(unittest.TestCase):
    def test_closing_the_job_kills_only_its_process(self):
        mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            job = headless.KillOnCloseJob()
            self.assertTrue(job.assign(int(mine._handle)))
            self.assertIsNone(mine.poll())
            job.close()
            mine.wait(timeout=20)
            self.assertIsNotNone(mine.poll())
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            mine.kill()
            other.wait()
            mine.wait()

    def test_run_game_refuses_bad_map(self):
        class S:
            name = "Fake"
        with self.assertRaises(headless.HeadlessError):
            headless.run_game(S(), {"op": "game_test"}, map_path="-userdir=C:/real")


# ---------------------------------------------------------------- bpscan

class BpscanTests(unittest.TestCase):
    def test_filter_download_sends_browser_user_agent(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"# ABF-BlueprintFilter\nAllow /Script/Engine\n"

        def fake_urlopen(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return Resp()

        target = Path(tempfile.mkdtemp(prefix="abfflt")) / "data" / "BlueprintFilter.txt"
        try:
            with mock.patch.object(bpscan, "FILTER_FILE", target), mock.patch.object(bpscan.urllib.request, "urlopen", fake_urlopen):
                rules, meta = bpscan.load_filter()
            self.assertEqual(seen["ua"], bpscan.BROWSER_UA)
            self.assertIn("Mozilla/5.0", seen["ua"])
            self.assertEqual(rules["/Script/Engine"], "Allow")
        finally:
            shutil.rmtree(target.parent.parent, ignore_errors=True)

    def _export(self):
        e = t3d.entry_node("F", [{"name": "In", "type": "int"}])
        r = t3d.result_node("F", [{"name": "Out", "type": "int"}])
        m = GraphModel()
        m.add(e)
        m.add(r)
        m.wire(e.name, "then", r.name, "execute")
        lines = ['Begin Object Class=/Script/Engine.Blueprint Name="BP_X" ExportPath="x"',
                 '   Begin Object Class=/Script/Engine.EdGraph Name="F" ExportPath="x"', "   End Object",
                 '   Begin Object Name="F" ExportPath="x"', t3d.graph_text(m.defs(), 6), "   End Object",
                 '   ParentClass="/Script/CoreUObject.Class\'/Script/Engine.Actor\'"',
                 '   NewVariables(0)=' + t3d.var_line("Score", "int", guid="G", default="10"),
                 '   NewVariables(1)=' + t3d.var_line("Items", "array:object:/Script/Engine.Actor", guid="G2"),
                 "   FunctionGraphs(0)=\"/Script/Engine.EdGraph'F'\"",
                 "End Object"]
        return "\n".join(lines)

    def test_parse_structure(self):
        s = bpscan.parse_structure(self._export())
        self.assertEqual(s["blueprint"]["name"], "BP_X")
        self.assertEqual(s["blueprint"]["parent_class"], "/Script/Engine.Actor")
        self.assertEqual(s["variables"][0], {"name": "Score", "type": "int", "sub_object": None, "default": "10"})
        self.assertEqual(s["variables"][1]["type"], "array:object:/Script/Engine.Actor")
        self.assertEqual(s["functions"], [{"name": "F", "inputs": [{"name": "In", "type": "int"}], "outputs": [{"name": "Out", "type": "int"}]}])
        nodes = {n["name"]: n for n in s["graphs"]["F"]}
        self.assertEqual(set(nodes), {"K2Node_FunctionEntry_0", "K2Node_FunctionResult_0"})
        then = [p for p in nodes["K2Node_FunctionEntry_0"]["pins"] if p["name"] == "then"][0]
        self.assertEqual(then["links"][0]["node"], "K2Node_FunctionResult_0")
        self.assertEqual(then["links"][0]["pin"], "execute")
        self.assertEqual(nodes["K2Node_FunctionEntry_0"]["pos"], [0, 0])
        self.assertTrue(nodes["K2Node_FunctionEntry_0"]["guid"])

    def test_parse_t3d_keys_unchanged(self):
        r = bpscan.parse_t3d(self._export())
        self.assertEqual(set(r), {"blueprint", "graphs", "nodes"})
        n = r["nodes"][0]
        for key in ("name", "class", "graph", "pins", "object_refs", "function"):
            self.assertIn(key, n)
        for key in ("name", "type", "dir"):
            self.assertIn(key, n["pins"][0])


# ---------------------------------------------------------------- safety

class SafetyTests(unittest.TestCase):
    FORBIDDEN = ["wininput.click", "wininput.keys", "wininput.focus", "wininput.type_text", "set_clipboard", "mouse_event",
                 "keybd_event", "SendInput", "SetCursorPos", "SetForegroundWindow", "SwitchDesktop", "delete_asset", "delete_directory"]

    def files(self):
        out = [ROOT / n for n in ("headless.py", "hiddendesk.py", "staging.py", "headless_tools.py")]
        out += sorted((ROOT / "editor" / "bpkit").glob("*.py"))
        return out

    def test_no_input_automation_or_deletes(self):
        for f in self.files():
            text = f.read_text(encoding="utf-8")
            for token in self.FORBIDDEN:
                self.assertNotIn(token, text, "%s contains %s" % (f.name, token))

    def test_reparent_only_in_scratch_helper(self):
        for f in self.files():
            lines = f.read_text(encoding="utf-8").splitlines()
            for i, l in enumerate(lines):
                if "reparent_blueprint" in l:
                    self.assertEqual(f.name, "graphs.py")
                    start = max(j for j in range(i + 1) if lines[j].startswith("def "))
                    self.assertTrue(lines[start].startswith("def _rebuild_scratch_pins"), (f.name, i))

    def test_cursor_patch_guard(self):
        text = (ROOT / "editor" / "bpkit" / "cursorpatch.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("ABF_MCP_HIDDEN") != "1"', text)
        startup = (ROOT / "editor" / "abf_mcp_startup.py").read_text(encoding="utf-8")
        self.assertIn('if os.environ.get("ABF_MCP_HIDDEN") == "1":', startup)

    def test_pure_layer_has_no_unreal(self):
        for n in ("t3d.py", "layout.py", "specs.py"):
            self.assertNotIn("import unreal", (ROOT / "editor" / "bpkit" / n).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- stdio smoke

class StdioSmokeTests(unittest.TestCase):
    def test_tools_list_and_unknown_mod(self):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "staging_status", "arguments": {"mod": "NoSuchMod"}}}]
        env = dict(os.environ, ABF_HEADLESS_DIR=_TMP)
        p = subprocess.run([sys.executable, "-I", str(ROOT / "server.py")], input="\n".join(json.dumps(r) for r in reqs) + "\n",
                           capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
        replies = {json.loads(l)["id"]: json.loads(l) for l in p.stdout.splitlines() if l.strip()}
        self.assertEqual(replies[1]["result"]["serverInfo"]["version"], "2.3.0")
        names = [t["name"] for t in replies[2]["result"]["tools"]]
        self.assertEqual(len(names), 42)
        for n in ("staging_sync", "staging_status", "bp_apply", "bp_read", "bp_compile_report", "bp_game_test",
                  "hidden_editor_shot", "mod_audit", "bp_publish"):
            self.assertIn(n, names)
        self.assertTrue(replies[3]["result"]["isError"])
        self.assertIn("staging_sync", replies[3]["result"]["content"][0]["text"])
        self.assertIn("paste_graph is the older route", replies[1]["result"]["instructions"])


if __name__ == "__main__":
    unittest.main()
