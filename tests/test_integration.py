"""Integration tests against hidden Unreal processes on TEMPORARY copies only.

    ABF_RUN_INTEGRATION=1 ABF_HEADLESS_DIR=<temp folder without spaces> python -m unittest tests.test_integration -v

Expect 30+ minutes. Needs the Mod Editor kit installed. Nothing here touches your editor or your mods: the fixture mod is
generated under a temp folder, run_json is a function that raises (so bp_publish sees "no editor"), and the real-mod copy of
test 17 is only used when ABF_TEST_REAL_COPY points at a temp copy you made yourself.
Tests run in numeric order and share the staged fixture mod.
"""
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "editor"))

import paths  # noqa: E402

ENABLED = os.environ.get("ABF_RUN_INTEGRATION") == "1" and paths.find_editor_root()
MOD = "McpFixture"
T = "/%s/T/" % MOD


def t3d_errors(text, prefix=None):
    problems = []
    for m in re.finditer(r'Begin Object Class=([^\s]+) Name="([^"]+)"(.*?)\n\s*End Object', text, re.S):
        cls, name, body = m.group(1), m.group(2), m.group(3)
        if "Node" not in cls.split(".")[-1]:
            continue
        err = re.search(r'ErrorMsg="(.*)"', body)
        orphan = re.findall(r'PinName="([^"]+)"[^\n]*bOrphanedPin=True', body)
        if err or orphan:
            problems.append({"node": name, "error": err.group(1)[:300] if err else None, "orphaned_pins": orphan})
    return {"problems": problems}


def no_editor(*args, **kwargs):
    raise RuntimeError("test: the user's editor is never contacted")


def png_not_blank(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    pos, idat = 8, b""
    while pos < len(data):
        n = int.from_bytes(data[pos:pos + 4], "big")
        tag = data[pos + 4:pos + 8]
        if tag == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    return len(set(raw[:200000])) > 8


@unittest.skipUnless(ENABLED, "set ABF_RUN_INTEGRATION=1 with the Mod Editor installed")
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="abfint"))
        if " " in str(cls.work):
            raise unittest.SkipTest("temp folder has a space")
        if not os.environ.get("ABF_HEADLESS_DIR"):
            os.environ["ABF_HEADLESS_DIR"] = str(cls.work / "headless")
        import headless_tools
        import staging
        cls.staging = staging
        cls.tools = {t[0]: t[1] for t in headless_tools.build(no_editor, (RuntimeError,), t3d_errors, ROOT / "editor", [paths.find_editor_root()], "", list_processes=lambda: [])}
        cls.real = cls.work / "real" / MOD
        (cls.real / "Content").mkdir(parents=True)
        (cls.real / (MOD + ".uplugin")).write_text(json.dumps(
            {"FileVersion": 3, "Version": 1, "VersionName": "1.0", "FriendlyName": MOD, "Category": "UGC", "CanContainContent": True,
             "NoCode": True, "Installed": False, "ExplicitlyLoaded": True}), encoding="utf-8")
        (cls.real / (MOD + ".abfmod")).write_text('{"ModId":0,"TestingModId":0}')
        cls.stage = staging.sync(cls.real, discard_changes=True)
        seed = {"asset": T + "BP_Seed", "create": {"class": "Blueprint", "parent": "/Script/Engine.Actor", "if_missing": True},
                "raw": [{"kind": "python", "code": (
                    "tools = unreal.AssetToolsHelpers.get_asset_tools()\n"
                    "cfg = tools.create_asset('AbioticModConfig', '/%s', unreal.AbioticModConfiguration, unreal.DataAssetFactory())\n"
                    "meta = tools.create_asset('UUGC_Metadata', '/%s', unreal.UGC_Metadata, unreal.DataAssetFactory())\n"
                    "assert cfg is not None and meta is not None\n"
                    "unreal.EditorAssetLibrary.save_loaded_asset(cfg, False)\n"
                    "unreal.EditorAssetLibrary.save_loaded_asset(meta, False)\n") % (MOD, MOD)}]}
        r = cls.apply([seed])
        assert r["ok"], r

    @classmethod
    def tearDownClass(cls):
        # mod_audit / hidden_editor_shot made <root>/home/AppData/{Local,Roaming}: junctions to the REAL AppData. Unlink them
        # first; a recursive delete must never get near them (older interpreters and PowerShell 5.1 follow junctions).
        import hiddendesk
        _removed, still = hiddendesk.remove_home()
        if still:
            raise RuntimeError("not deleting %s: these AppData links could not be removed, delete them by hand first: %s" % (cls.work, still))
        shutil.rmtree(cls.work, ignore_errors=True)

    # ------------------------------------------------------------ helpers
    @classmethod
    def call(cls, name, **args):
        res = cls.tools[name](args)
        if isinstance(res, dict):
            return res, res.get("isError")
        text, is_err = res
        try:
            return json.loads(text), is_err
        except ValueError:
            return {"text": text}, is_err

    @classmethod
    def apply(cls, assets, **extra):
        r, _ = cls.call("bp_apply", mod=MOD, assets=assets, **extra)
        return r

    def apply_ok(self, assets):
        r = self.apply(assets)
        self.assertTrue(r["ok"], json.dumps(r, indent=1)[:3000])
        return r

    def game(self, steps, **extra):
        r, _ = self.call("bp_game_test", mod=MOD, steps=steps, **extra)
        self.assertTrue(r.get("ok"), json.dumps(r, indent=1)[:4000])
        return r

    def read(self, asset, graph=None, pins=False):
        args = dict(mod=MOD, asset_path=asset, include_pins=pins)
        if graph:
            args["graph"] = graph
        r, err = self.call("bp_read", **args)
        self.assertFalse(err, r)
        return r

    @staticmethod
    def fn(name, ins, outs, nodes, wires, **extra):
        d = {"name": name, "inputs": ins, "outputs": outs, "nodes": nodes, "wires": wires}
        d.update(extra)
        return d

    @staticmethod
    def new_bp(name, **spec):
        d = {"asset": T + name, "create": {"class": "Blueprint", "parent": "/Script/Engine.Actor", "if_missing": True}}
        d.update(spec)
        return d

    # ------------------------------------------------------------ tests
    def test_01_variables_and_function(self):
        spec = self.new_bp("BP_V",
                           variables=[{"name": "Score", "type": "int", "default": "10"}, {"name": "Speed", "type": "real", "default": "2.5"},
                                      {"name": "Label", "type": "string", "default": "hello"}],
                           functions=[self.fn("AddScore", [{"name": "Amount", "type": "int"}], [{"name": "NewTotal", "type": "int"}],
                                              [{"id": "g", "k": "get", "name": "Score"},
                                               {"id": "add", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Add_IntInt"},
                                               {"id": "s", "k": "set", "name": "Score"}],
                                              [["entry.then", "s.execute"], ["g.Score", "add.A"], ["entry.Amount", "add.B"],
                                               ["add.ReturnValue", "s.Score"], ["s.then", "result.execute"], ["s.Output_Get", "result.NewTotal"]])])
        self.apply_ok([spec])
        self.game([{"op": "new", "class": T + "BP_V", "as": "x"},
                   {"op": "get", "target": "$x", "property": "Score", "as": "start"},
                   {"op": "assert", "value": "$start", "equals": 10},
                   {"op": "call", "target": "$x", "function": "AddScore", "args": [5], "as": "r1"},
                   {"op": "assert", "value": "$r1", "equals": 15},
                   {"op": "call", "target": "$x", "function": "AddScore", "args": [7], "as": "r2"},
                   {"op": "assert", "value": "$r2", "equals": 22}])
        st = self.read(T + "BP_V")
        d = {v["name"]: v for v in st["variables"]}
        self.assertEqual(d["Score"]["default"], "10")
        self.assertEqual(float(d["Speed"]["default"]), 2.5)
        self.assertEqual(d["Label"]["default"], "hello")
        for v in d.values():
            self.assertEqual(v["default_status"], "ok", v)
        self.assertEqual(st["defaults_status"], "ok")
        self.assertEqual(st["components_status"], "ok")
        self.assertEqual([c for c in st["components"] if not c["default_scene_root"]], [])

    def test_02_components(self):
        base = self.new_bp("BP_C4",
                           components=[{"name": "LabMesh", "class": "/Script/Engine.StaticMeshComponent"},
                                       {"name": "LabBox", "class": "/Script/Engine.BoxComponent", "parent": "LabMesh",
                                        "properties": {"component_tags": ["LabTag"]},
                                        "expressions": {"box_extent": "unreal.Vector(11,22,33)", "relative_location": "unreal.Vector(1,2,3)"}}],
                           functions=[self.fn("HideBox", [], [],
                                              [{"id": "v", "k": "get", "name": "LabBox"},
                                               {"id": "c", "k": "call", "class": "/Script/Engine.SceneComponent", "function": "SetVisibility",
                                                "set": {"bNewVisibility": "false"}}],
                                              [["entry.then", "c.execute"], ["v.LabBox", "c.self"]])])
        spawner = self.new_bp("BP_S4", functions=[self.fn(
            "SpawnIt", [], [{"name": "Spawned", "type": "object:/Script/Engine.Actor"}],
            [{"id": "mt", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "MakeTransform"},
             {"id": "sp", "k": "spawn", "objects": {"Class": T + "BP_C4.BP_C4_C"}}],
            [["entry.then", "sp.execute"], ["sp.then", "result.execute"], ["mt.ReturnValue", "sp.SpawnTransform"], ["sp.ReturnValue", "result.Spawned"]])])
        self.apply_ok([base])
        self.apply_ok([spawner])
        check = ("b = ctx['a'].get_editor_property('LabBox')\next = b.get_unscaled_box_extent()\n"
                 "result = (abs(ext.x-11)<0.01 and abs(ext.y-22)<0.01 and abs(ext.z-33)<0.01 and list(b.get_editor_property('component_tags'))==['LabTag'] "
                 "and b.get_attach_parent().get_name()=='LabMesh')\n")
        self.game([{"op": "new", "class": T + "BP_S4", "as": "h"},
                   {"op": "call", "target": "$h", "function": "SpawnIt", "as": "a"},
                   {"op": "assert", "value": "$a", "not_none": True},
                   {"op": "python", "code": check, "as": "ok"},
                   {"op": "assert", "value": "$ok", "truthy": True},
                   {"op": "call", "target": "$a", "function": "HideBox"},
                   {"op": "python", "code": "result = ctx['a'].get_editor_property('LabBox').get_editor_property('visible') is False", "as": "hid"},
                   {"op": "assert", "value": "$hid", "truthy": True}])
        st = self.read(T + "BP_C4")
        comps = {c["name"]: c for c in st["components"]}
        self.assertTrue({"LabMesh", "LabBox"} <= set(comps), comps)
        self.assertEqual(comps["LabBox"]["class"], "/Script/Engine.BoxComponent")
        self.assertEqual(comps["LabBox"]["parent"], "LabMesh")
        self.assertFalse(comps["LabMesh"]["inherited"])
        self.assertFalse(comps["LabBox"]["inherited"])

    def test_03_widget_tree(self):
        tree = {"root": {"class": "VerticalBox", "name": "RootBox", "children": [
            {"class": "TextBlock", "name": "TitleText", "text": "Hello Title", "slot": {"Padding": "(Left=7.000000,Top=3.000000)"}},
            {"class": "TextBlock", "name": "BodyText", "text": "Body Default"}]}}
        spec = {"asset": T + "WBP_W", "create": {"class": "WidgetBlueprint", "if_missing": True}, "widget_tree": tree,
                "functions": [self.fn("SetBody", [{"name": "Msg", "type": "text"}], [],
                                      [{"id": "v", "k": "get", "name": "BodyText"},
                                       {"id": "c", "k": "call", "class": "/Script/UMG.TextBlock", "function": "SetText"}],
                                      [["entry.then", "c.execute"], ["v.BodyText", "c.self"], ["entry.Msg", "c.InText"]])]}
        self.apply_ok([spec])
        code = ("w = ctx['w']\nroot = w.get_editor_property('RootBox')\nt = w.get_editor_property('TitleText')\nb = w.get_editor_property('BodyText')\n"
                "pad = t.slot.get_editor_property('padding')\n"
                "result = (str(t.get_text())=='Hello Title' and root.get_children_count()==2 and abs(pad.left-7)<0.01 and abs(pad.top-3)<0.01 "
                "and t.get_parent().get_name()=='RootBox')\n")
        self.game([{"op": "widget", "class": T + "WBP_W", "as": "w"},
                   {"op": "python", "code": code, "as": "ok"}, {"op": "assert", "value": "$ok", "truthy": True},
                   {"op": "call", "target": "$w", "function": "SetBody", "args": [{"text": "Changed by Blueprint"}]},
                   {"op": "python", "code": "result = str(ctx['w'].get_editor_property('BodyText').get_text())", "as": "body"},
                   {"op": "assert", "value": "$body", "equals": "Changed by Blueprint"}])
        st = self.read(T + "WBP_W")
        self.assertEqual(st["components"], [])
        self.assertEqual(st["components_status"], "ok")
        self.assertIn("not an Actor", st["components_note"])
        self.assertTrue({"RootBox", "TitleText", "BodyText"} <= {w["name"] for w in st["widgets"]})

    def test_04_canvas(self):
        tree = {"root": {"class": "CanvasPanel", "name": "Root", "children": [
            {"class": "Image", "name": "Bg", "slot": {"LayoutData": "(Offsets=(Left=10.000000,Top=20.000000,Right=300.000000,Bottom=40.000000),"
                                                                       "Anchors=(Minimum=(X=0.500000,Y=0.250000),Maximum=(X=0.500000,Y=0.250000)),Alignment=(X=0.500000,Y=1.000000))",
                                                      "bAutoSize": "True", "ZOrder": "3"}},
            {"class": "HorizontalBox", "name": "Row", "children": [
                {"class": "TextBlock", "name": "Lbl", "text": "Canvas Label", "slot": {"Size": "(SizeRule=Fill)"}},
                {"class": "ProgressBar", "name": "Bar", "props": {"Percent": "0.420000"}}]}]}}
        self.apply_ok([{"asset": T + "WBP_Canvas", "create": {"class": "WidgetBlueprint", "if_missing": True}, "widget_tree": tree}])
        r, _ = self.call("bp_compile_report", mod=MOD, asset_paths=[T + "WBP_Canvas"])
        self.assertTrue(r["clean"], r)
        self.assertEqual({w["name"] for w in self.read(T + "WBP_Canvas")["widgets"]}, {"Root", "Bg", "Row", "Lbl", "Bar"})

    def test_05_macro(self):
        spec = self.new_bp("BP_M",
                           macros=[{"name": "LabMacro", "inputs": [{"name": "Exec", "type": "exec"}, {"name": "A", "type": "int"}],
                                    "outputs": [{"name": "Then", "type": "exec"}, {"name": "R", "type": "int"}],
                                    "nodes": [{"id": "p", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Add_IntInt", "set": {"B": "1"}}],
                                    "wires": [["in.A", "p.A"], ["p.ReturnValue", "out.R"], ["in.Exec", "out.Then"]]}],
                           functions=[self.fn("UseMacro", [{"name": "V", "type": "int"}], [{"name": "Out", "type": "int"}],
                                              [{"id": "m", "k": "macro", "name": "LabMacro", "inputs": [{"name": "Exec", "type": "exec"}, {"name": "A", "type": "int"}],
                                                "outputs": [{"name": "Then", "type": "exec"}, {"name": "R", "type": "int"}]}],
                                              [["entry.then", "m.Exec"], ["entry.V", "m.A"], ["m.Then", "result.execute"], ["m.R", "result.Out"]])])
        self.apply_ok([spec])
        self.game([{"op": "new", "class": T + "BP_M", "as": "m"},
                   {"op": "call", "target": "$m", "function": "UseMacro", "args": [4], "as": "r"},
                   {"op": "assert", "value": "$r", "equals": 5}])

    def test_06_dispatcher(self):
        P = [{"name": "Amount", "type": "int"}]
        spec = self.new_bp("BP_D", variables=[{"name": "Count", "type": "int", "default": "0"}], dispatchers=[{"name": "OnPing", "params": P}],
                           graphs=[{"graph": "EventGraph", "add": [
                               {"id": "b", "k": "event", "name": "BindIt"},
                               {"id": "a", "k": "bind_dispatcher", "name": "OnPing", "params": P},
                               {"id": "fi", "k": "event", "name": "Fire", "params": [{"name": "Amt", "type": "int"}]},
                               {"id": "c", "k": "call_dispatcher", "name": "OnPing", "params": P},
                               {"id": "gt", "k": "event", "name": "OnGot", "params": P},
                               {"id": "g1", "k": "get", "name": "Count"},
                               {"id": "p", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Add_IntInt"},
                               {"id": "s", "k": "set", "name": "Count"}],
                               "wires": [["b.then", "a.execute"], ["gt.OutputDelegate", "a.Delegate"], ["fi.then", "c.execute"], ["fi.Amt", "c.Amount"],
                                         ["gt.then", "s.execute"], ["g1.Count", "p.A"], ["gt.Amount", "p.B"], ["p.ReturnValue", "s.Count"]]}])
        self.apply_ok([spec])
        self.game([{"op": "new", "class": T + "BP_D", "as": "d"},
                   {"op": "call", "target": "$d", "function": "BindIt"},
                   {"op": "call", "target": "$d", "function": "Fire", "args": [5]},
                   {"op": "call", "target": "$d", "function": "Fire", "args": [7]},
                   {"op": "get", "target": "$d", "property": "Count", "as": "n"},
                   {"op": "assert", "value": "$n", "equals": 12}])
        d = {v["name"]: v for v in self.read(T + "BP_D")["variables"]}
        self.assertEqual(d["Count"]["default"], "0")
        self.assertEqual(d["OnPing"]["default_status"], "not_applicable")

    def test_07_interface(self):
        io = [{"name": "In", "type": "int"}], [{"name": "Out", "type": "int"}]
        self.apply_ok([{"asset": T + "BPI_T", "create": {"class": "BlueprintInterface", "if_missing": True},
                        "functions": [self.fn("Ping", io[0], io[1], [], [])]}])
        spec = self.new_bp("BP_I", interfaces=[{"class": T + "BPI_T.BPI_T_C", "functions": [self.fn(
            "Ping", io[0], io[1], [{"id": "m", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Multiply_IntInt", "set": {"B": "2"}}],
            [["entry.In", "m.A"], ["entry.then", "result.execute"], ["m.ReturnValue", "result.Out"]])]}],
                           functions=[self.fn("CallPing", [{"name": "V", "type": "int"}], [{"name": "O", "type": "int"}],
                                              [{"id": "sf", "k": "self"}, {"id": "c", "k": "message", "function": "Ping", "interface": T + "BPI_T.BPI_T_C"}],
                                              [["entry.then", "c.execute"], ["sf.self", "c.self"], ["entry.V", "c.In"], ["c.then", "result.execute"], ["c.Out", "result.O"]])])
        self.apply_ok([spec])
        self.game([{"op": "new", "class": T + "BP_I", "as": "i"},
                   {"op": "call", "target": "$i", "function": "Ping", "args": [21], "as": "p"},
                   {"op": "assert", "value": "$p", "equals": 42},
                   {"op": "call", "target": "$i", "function": "CallPing", "args": [4], "as": "q"},
                   {"op": "assert", "value": "$q", "equals": 8}])

    def test_08_timeline(self):
        spec = self.new_bp("BP_TL", variables=[{"name": "Cur", "type": "real", "default": "-1.0"}, {"name": "Done", "type": "bool", "default": "false"}],
                           timelines=[{"name": "LabT", "length": 1.0, "loop": False, "tracks": [{"name": "Val", "keys": [[0, 0], [1, 10]]}]}],
                           graphs=[{"graph": "EventGraph", "add": [
                               {"id": "go", "k": "event", "name": "Go"}, {"id": "t", "k": "timeline", "name": "LabT"},
                               {"id": "s1", "k": "set", "name": "Cur"}, {"id": "s2", "k": "set", "name": "Done", "set": {"Done": "true"}}],
                               "wires": [["go.then", "t.PlayFromStart"], ["t.Update", "s1.execute"], ["t.Val", "s1.Cur"], ["t.Finished", "s2.execute"]]}])
        self.apply_ok([spec])
        self.game([{"op": "spawn", "class": T + "BP_TL", "as": "a"}, {"op": "call", "target": "$a", "function": "Go"}, {"op": "wait", "seconds": 4},
                   {"op": "get", "target": "$a", "property": "Cur", "as": "cur"}, {"op": "assert", "value": "$cur", "approx": 10.0, "tolerance": 0.1},
                   {"op": "get", "target": "$a", "property": "Done", "as": "done"}, {"op": "assert", "value": "$done", "truthy": True}])

    def test_09_widget_animation(self):
        tree = {"root": {"class": "TextBlock", "name": "Txt", "text": "Animated"}}
        spec = {"asset": T + "WBP_Anim", "create": {"class": "WidgetBlueprint", "if_missing": True}, "widget_tree": tree,
                "animations": [{"name": "FadeIn", "fps": 20, "end_frame": 20, "tracks": [{"widget": "Txt", "property": "RenderOpacity", "keys": [[0, 0.0], [20, 1.0]]}]}]}
        self.apply_ok([spec])
        self.game([{"op": "widget", "class": T + "WBP_Anim", "viewport": True, "as": "w"},
                   {"op": "python", "code": "ctx['w'].play_animation(ctx['w'].get_editor_property('FadeIn'))\nresult = True"},
                   {"op": "wait", "seconds": 0.3},
                   {"op": "python", "code": "result = ctx['w'].get_editor_property('Txt').get_render_opacity()", "as": "early"},
                   {"op": "wait", "seconds": 1.5},
                   {"op": "python", "code": "result = ctx['w'].get_editor_property('Txt').get_render_opacity()", "as": "late"},
                   {"op": "python", "code": "result = ctx['early'] < ctx['late'] and ctx['late'] > 0.9", "as": "rises"},
                   {"op": "assert", "value": "$rises", "truthy": True}])

    def test_10_edit_existing(self):
        first = self.apply_ok([self.new_bp(
            "BP_E", variables=[{"name": "Count", "type": "int", "default": "0"}],
            functions=[self.fn("Bump", [], [], [{"id": "g", "k": "get", "name": "Count"},
                                                {"id": "p", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Add_IntInt", "set": {"B": "2"}},
                                                {"id": "s", "k": "set", "name": "Count"}],
                               [["entry.then", "s.execute"], ["g.Count", "p.A"], ["p.ReturnValue", "s.Count"]])],
            graphs=[{"graph": "EventGraph", "add": [{"id": "e", "k": "event", "name": "Hi"}, {"id": "s2", "k": "set", "name": "Count", "set": {"Count": "3"}}],
                     "wires": [["e.then", "s2.execute"]]}])])
        before = self.read(T + "BP_E")
        ids = first["assets"][0]["node_ids"]["fn:Bump"]
        self.assertEqual(len(before["graphs"]["Bump"]), 4)
        self.apply_ok([{"asset": T + "BP_E", "graphs": [{"graph": "Bump", "remove": [ids["p"]], "wires": [[ids["g"] + ".Count", ids["s"] + ".Count"]]}]}])
        after = self.read(T + "BP_E")
        nodes = {n["name"]: n for n in after["graphs"]["Bump"]}
        self.assertEqual(len(nodes), 3)
        get_pin = [p for p in nodes[ids["g"]]["pins"] if p["name"] == "Count"] if "pins" in nodes[ids["g"]] else None
        full = self.read(T + "BP_E", graph="Bump", pins=True)
        nodes = {n["name"]: n for n in full["graphs"]["Bump"]}
        self.assertEqual([l["node"] for p in nodes[ids["g"]]["pins"] if p["name"] == "Count" for l in p["links"]], [ids["s"]])
        del get_pin

        def counts():
            g = self.read(T + "BP_E", graph="EventGraph", pins=True)["graphs"]["EventGraph"]
            return len(g), sum(len(p["links"]) for n in g for p in n["pins"])
        c0 = counts()
        self.apply_ok([{"asset": T + "BP_E", "graphs": [{"graph": "EventGraph"}]}])
        self.assertEqual(counts(), c0)

    def test_11_more_coverage(self):
        spec = self.new_bp(
            "BP_N", variables=[{"name": "Flag", "type": "int", "default": "0"}, {"name": "Dead", "type": "int", "default": "0"},
                               {"name": "Items", "type": "array:int"}],
            components=[{"name": "LabBox", "class": "/Script/Engine.BoxComponent"}],
            functions=[self.fn("Twice", [{"name": "X", "type": "int"}], [{"name": "R", "type": "int"}],
                               [{"id": "m", "k": "call", "class": "/Script/Engine.KismetMathLibrary", "function": "Multiply_IntInt", "set": {"B": "2"}}],
                               [["entry.then", "result.execute"], ["entry.X", "m.A"], ["m.ReturnValue", "result.R"]]),
                       self.fn("UseTwice", [{"name": "V", "type": "int"}], [{"name": "O", "type": "int"}],
                               [{"id": "c", "k": "call", "function": "Twice"}],
                               [["entry.then", "c.execute"], ["c.then", "result.execute"], ["entry.V", "c.X"], ["c.R", "result.O"]])],
            graphs=[{"graph": "EventGraph", "add": [{"id": "o", "k": "override", "function": "ReceiveBeginPlay", "class": "/Script/Engine.Actor"},
                                                    {"id": "s", "k": "set", "name": "Flag", "set": {"Flag": "7"}}],
                     "wires": [["o.then", "s.execute"]]}])
        self.apply_ok([spec])
        events = [n for n in self.read(T + "BP_N", graph="EventGraph")["graphs"]["EventGraph"] if n["class"] == "K2Node_Event"]
        self.assertEqual(len([n for n in events if n.get("event", {}).get("name") == "ReceiveBeginPlay"]), 1, "the ghost node must be reused")
        self.game([{"op": "spawn", "class": T + "BP_N", "as": "a"}, {"op": "wait", "seconds": 1.0},
                   {"op": "get", "target": "$a", "property": "Flag", "as": "f"}, {"op": "assert", "value": "$f", "equals": 7},
                   {"op": "call", "target": "$a", "function": "UseTwice", "args": [21], "as": "t"}, {"op": "assert", "value": "$t", "equals": 42}])
        st = self.read(T + "BP_N")
        self.assertEqual({v["name"]: v["type"] for v in st["variables"]}["Items"], "array:int")
        self.assertEqual({c["name"]: c["parent"] for c in st["components"]}.get("LabBox"), "DefaultSceneRoot")
        items = {v["name"]: v for v in st["variables"]}["Items"]
        self.assertEqual((items["default"], items["default_status"]), ("()", "ok"))
        # remove a variable and a component
        self.apply_ok([{"asset": T + "BP_N", "remove_variables": ["Dead"], "components": [{"name": "LabBox", "remove": True}]}])
        st = self.read(T + "BP_N")
        self.assertEqual(sorted(v["name"] for v in st["variables"] if v["name"] in ("Flag", "Dead", "Items")), ["Flag", "Items"])
        self.assertEqual(st["components_status"], "ok")
        self.assertIsInstance(st["components"], list)
        self.assertNotIn("LabBox", [c["name"] for c in st["components"]])
        # two timeline tracks
        self.apply_ok([self.new_bp("BP_TL2", timelines=[{"name": "Two", "length": 2.0, "tracks": [
            {"name": "A", "keys": [[0, 0], [2, 1]]}, {"name": "B", "keys": [[0, 5], [2, 6]]}]}],
                                   graphs=[{"graph": "EventGraph", "add": [{"id": "t", "k": "timeline", "name": "Two"}]}])])
        pins = self.read(T + "BP_TL2", graph="EventGraph", pins=True)["graphs"]["EventGraph"]
        tl = [n for n in pins if n["class"] == "K2Node_Timeline"][0]
        self.assertTrue({"A", "B", "Update", "Finished"} <= {p["name"] for p in tl["pins"]})
        # a second interface appended without losing the first
        self.apply_ok([{"asset": T + "BPI_U", "create": {"class": "BlueprintInterface", "if_missing": True},
                        "functions": [self.fn("Pong", [], [], [], [])]}])
        self.apply_ok([{"asset": T + "BP_I", "interfaces": [{"class": T + "BPI_U.BPI_U_C", "functions": [self.fn("Pong", [], [], [], [])]}]}])
        names = " ".join(self.read(T + "BP_I")["interfaces"])
        self.assertIn("BPI_T", names)
        self.assertIn("BPI_U", names)
        self.game([{"op": "new", "class": T + "BP_I", "as": "i"}, {"op": "call", "target": "$i", "function": "Ping", "args": [3], "as": "p"},
                   {"op": "assert", "value": "$p", "equals": 6}])
        # a Blueprint whose parent is not an Actor has no components: [] with a note, not unavailable
        self.apply_ok([{"asset": T + "BP_Obj", "create": {"class": "Blueprint", "parent": "/Script/CoreUObject.Object", "if_missing": True},
                        "variables": [{"name": "N", "type": "int", "default": "3"}]}])
        st = self.read(T + "BP_Obj")
        self.assertEqual(st["components"], [])
        self.assertEqual(st["components_status"], "ok")
        self.assertTrue(st["components_note"])
        self.assertEqual({v["name"]: v for v in st["variables"]}["N"]["default"], "3")

    def test_12_rollback(self):
        def snapshot():
            return {p.relative_to(self.stage.dir).as_posix(): p.read_bytes() for p in (self.stage.dir / "Content").rglob("*") if p.is_file()}
        before = snapshot()
        bad = {"asset": T + "BP_V", "functions": [self.fn("Broken", [], [], [
            {"id": "c", "k": "call", "class": "/Script/Engine.SceneComponent", "function": "SetVisibility"}], [["entry.then", "c.execute"]])]}
        r = self.apply([bad])
        self.assertFalse(r["ok"])
        self.assertTrue(r["rolled_back"], r)
        self.assertEqual(snapshot(), before)

    def test_13_layout(self):
        from bpkit import layout
        adds = [{"id": "e%d" % i, "k": "event", "name": "Ev%d" % i} for i in range(3)] + [
            {"id": "b%d" % i, "k": "branch"} for i in range(3)]
        wires = [["e0.then", "b0.execute"], ["e1.then", "b1.execute"], ["e2.then", "b2.execute"], ["b0.then", "b1.execute"]]
        self.apply_ok([self.new_bp("BP_L", graphs=[{"graph": "EventGraph", "add": adds, "wires": wires}])])
        nodes = self.read(T + "BP_L", graph="EventGraph", pins=True)["graphs"]["EventGraph"]
        self.assertGreaterEqual(len(nodes), 9)
        rects = []
        for n in nodes:
            ins = sum(1 for p in n["pins"] if p["dir"] == "in")
            w, h = layout.node_size(ins, len(n["pins"]) - ins)
            rects.append((n["name"], (n["pos"][0], n["pos"][1], w, h)))
        for i, (na, ra) in enumerate(rects):
            for nb, rb in rects[i + 1:]:
                self.assertFalse(layout.overlaps(ra, rb), "%s overlaps %s" % (na, nb))

    def test_14_hidden_editor_shot(self):
        r, err = self.call("hidden_editor_shot", mod=MOD, assets=[T + "BP_V"])
        self.assertFalse(err, json.dumps(r)[:2000] if isinstance(r, dict) else r)
        images = [c for c in r["content"] if c["type"] == "image"]
        self.assertTrue(images)
        for im in images:
            self.assertTrue(png_not_blank(base64.b64decode(im["data"])))

    def test_15_mod_audit(self):
        r, err = self.call("mod_audit", mod=MOD)
        self.assertFalse(err, json.dumps(r, indent=1)[:3000])
        self.assertTrue(r["completed"])
        self.assertEqual(r["errors"], 0)

    def test_16_publish_and_live_reload(self):
        # closed-editor route into the temp "real" mod
        dry, _ = self.call("bp_publish", mod=MOD, dry_run=True)
        self.assertTrue(dry["would_publish"], dry)
        r, err = self.call("bp_publish", mod=MOD)
        self.assertFalse(err, r)
        self.assertEqual(r["route"], "file-copy")
        self.assertTrue((self.real / "Content" / "T" / "BP_V.uasset").is_file())
        self.assertFalse((self.real / "Content" / "_McpTest").exists())
        self.assertFalse((self.real / "Content" / "_McpTmp").exists())
        again, _ = self.call("bp_publish", mod=MOD)
        self.assertEqual(again["published"], [])
        # hidden editor: modified asset refused, clean asset unloaded, replaced and reloaded
        import hiddendesk
        stage = self.staging.load(MOD)
        target = stage.dir / "Content" / "T" / "BP_V.uasset"
        self.assertFalse(list((stage.dir / "Content").rglob("*.uexp")), "assets have a .uexp: extend this test")
        v1 = target.read_bytes()
        self.apply_ok([{"asset": T + "BP_V", "variables": [{"name": "Extra", "type": "int"}]}])
        aside = self.work / "BP_V_v2.uasset"
        shutil.copyfile(target, aside)
        target.write_bytes(v1)
        job = {"op": "live_publish_test", "clean_asset": T + "BP_V", "dirty_asset": T + "BP_C4", "replacement": str(aside).replace("\\", "/"),
               "target": str(target).replace("\\", "/")}
        res = hiddendesk.run_editor(stage, job, timeout=420)
        self.assertTrue(res["ok"], json.dumps(res, indent=1, default=str)[:3000])
        out = res["result"]
        self.assertIn(T + "BP_C4", out["refused_dirty"]["refused"])
        self.assertEqual(out["prepared"]["refused"], [])
        self.assertEqual(out["reload"]["missing"], [])
        self.assertNotIn("Extra", out["vars_before"])
        self.assertIn("Extra", out["vars_after"])
        self.assertIn("Score", out["vars_after"])

    def test_17_real_copy(self):
        copy = os.environ.get("ABF_TEST_REAL_COPY")
        if not copy:
            self.skipTest("set ABF_TEST_REAL_COPY to a temp copy of a mod to run this")
        copy = Path(copy)
        self.assertTrue(str(copy).startswith(tempfile.gettempdir()) or str(copy).startswith(str(self.work)), "refusing a non-temp folder")
        r, err = self.call("staging_sync", mod_dir=str(copy), discard_changes=True)
        self.assertFalse(err, r)
        mod = r["mod"]
        graphs = {}
        assets = [p for p in (self.staging.load(mod).dir / "Content").rglob("*.uasset") if p.name.startswith(("BP", "WBP"))]
        self.assertTrue(assets)
        target = max(assets, key=lambda p: p.stat().st_size)
        rel = target.relative_to(self.staging.load(mod).dir / "Content").with_suffix("").as_posix()
        path = "/%s/%s" % (mod, rel)
        before = self.call("bp_read", mod=mod, asset_path=path)[0]
        name = "McpAudit%d" % len(before["variables"])
        ev = self.call("bp_apply", mod=mod, assets=[{"asset": path, "variables": [{"name": name, "type": "int"}],
                                                      "graphs": [{"graph": "EventGraph", "add": [{"id": "e", "k": "event", "name": "McpAuditEvent"}]}]}])[0]
        self.assertTrue(ev["ok"], json.dumps(ev, indent=1)[:2000])
        after = self.call("bp_read", mod=mod, asset_path=path)[0]
        self.assertTrue({v["name"] for v in before["variables"]} <= {v["name"] for v in after["variables"]})
        self.assertIn(name, [v["name"] for v in after["variables"]])
        audit, err = self.call("mod_audit", mod=mod)
        self.assertFalse(err, json.dumps(audit, indent=1)[:2000])
        del graphs


if __name__ == "__main__":
    unittest.main()
