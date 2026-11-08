"""Tests for the abfc extensions: no editor, no network, no real mod. Run with  python -m unittest tests.test_abfc_backend -v

Same bootstrap as tests/test_unit.py. Nothing here assumes backend_info exists or is absent.
"""
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
    _TMP = tempfile.mkdtemp(prefix="abfabfc")
    os.environ["ABF_HEADLESS_DIR"] = _TMP


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


import abfc_tools  # noqa: E402
import headless  # noqa: E402
import paths  # noqa: E402
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

    def test_final_compile_errors_ignore_the_states_before_the_last_compile(self):
        a, b = "/Mod/X/BP_A", "/Mod/X/BP_B"
        mark = lambda asset: "[x][0]LogPython: ABFMCP_APPLY_FINAL %s\n" % asset
        err = lambda msg, asset: "[x][0]LogBlueprint: Error: [Compiler] %s from Source: %s.%s\n" % (msg, asset, asset.rsplit("/", 1)[1])
        # the stage held an old error, the edit repaired it: the apply passes
        log = err("old call to a removed event", a) + mark(a)
        self.assertEqual(headless.final_compile_errors(log, "Mod", [a]), [])
        # an error the last compile still reports stays
        log = err("old", a) + mark(a) + err("still broken", a)
        self.assertEqual([e["message"] for e in headless.final_compile_errors(log, "Mod", [a])], ["still broken"])
        # each asset has its own mark, and a mark of BP_B is not one of BP_A
        log = err("a1", a) + mark(a) + err("b1", b) + mark(b) + err("b2", b)
        self.assertEqual([e["message"] for e in headless.final_compile_errors(log, "Mod", [a, b])], ["b2"])
        self.assertEqual(headless.final_compile_errors(mark("/Mod/X/BP_AA") + err("x", a), "Mod", [a])[0]["message"], "x")
        # scratch assets never count; a log without a mark (an older kit) is read whole
        self.assertEqual(headless.final_compile_errors(err("s", "/Mod/_McpTmp/BP_BkScratch1"), "Mod", ["/Mod/_McpTmp/BP_BkScratch1"]), [])
        self.assertEqual([e["message"] for e in headless.final_compile_errors(err("old", a), "Mod", [a])], ["old"])


class ToolTableTests(unittest.TestCase):
    def test_build_returns_exactly_three_tools_with_schemas(self):
        tools = abfc_tools.build("x")
        self.assertEqual([t[0] for t in tools], ["abfc_extensions", "reflect", "warm_session"])
        for name, fn, desc, props, required in tools:
            self.assertTrue(callable(fn))
            self.assertTrue(desc)
            self.assertIsInstance(props, dict)
            self.assertTrue(set(required) <= set(props))
        reflect = tools[1]
        self.assertEqual(reflect[4], ["mod", "classes"])
        for k in ("classes", "structs", "cast_targets"):
            self.assertEqual(reflect[3][k]["type"], "array")
        self.assertEqual(reflect[3]["meta"]["type"], "boolean")
        self.assertNotIn("meta", reflect[4])
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

    def test_function_meta_shape(self):
        tags = {"ToolTip": " Does it. ", "DisplayName": ""}

        def route(fn, tag):
            if fn == "bad":
                raise RuntimeError("no metadata")
            return tags[tag]

        self.assertEqual(reflectparse.read_function_meta(route, "ok"), {"tooltip": "Does it.", "displayName": None, "defaults": None})
        self.assertEqual(reflectparse.read_function_meta(route, "bad"), {"tooltip": None, "displayName": None, "defaults": None})

    def test_function_defaults_from_read_all(self):
        vals = {"ToolTip": "x", "CPP_Default_InString": "Hello", "CPP_Default_Duration": "2.000000", "CPP_Default_Key": ""}
        got = reflectparse.read_function_meta(lambda fn, tag: "", "f", read_all=lambda fn: vals)
        self.assertEqual(got["defaults"], {"InString": "Hello", "Duration": "2.000000"})
        self.assertEqual(reflectparse.read_function_meta(lambda fn, tag: "", "f", read_all=lambda fn: {})["defaults"], {})

    def test_function_defaults_fall_back_to_per_pin_reads(self):
        def read_all(fn):
            raise RuntimeError("no bulk read")

        def route(fn, tag):
            return {"CPP_Default_InString": "Hello", "CPP_Default_Key": ""}.get(tag, "")

        got = reflectparse.read_function_meta(route, "f", read_all=read_all, pins=["InString", "Key", "Other"])
        self.assertEqual(got["defaults"], {"InString": "Hello"})

    def test_function_defaults_unreadable_is_none(self):
        def boom(fn, tag):
            if tag.startswith("CPP_Default_"):
                raise RuntimeError("no")
            return ""

        def read_all(fn):
            raise RuntimeError("no")

        self.assertIsNone(reflectparse.read_function_meta(boom, "f", read_all=read_all, pins=["A"])["defaults"])
        self.assertIsNone(reflectparse.read_function_meta(boom, "f", read_all=read_all)["defaults"])

    def test_function_meta_time_cap(self):
        ticks = iter([0.0, 1.0, 25.0, 26.0])   # start, first check, second check (over budget), spare
        got = reflectparse.collect_function_meta(lambda fn, tag: tag + ":" + fn, [("a", "fa"), ("b", "fb")], budget=20.0,
                                                 clock=lambda: next(ticks))
        self.assertEqual(got["a"], {"tooltip": "ToolTip:fa", "displayName": "DisplayName:fa", "defaults": None})
        self.assertEqual(got["b"], {"tooltip": None, "displayName": None, "defaults": None})


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
        for n in ("reflect", "abfc_extensions", "warm_session"):
            self.assertNotIn(n, plain)
            self.assertIn(n, extra)
        self.assertEqual(len(set(extra)), len(extra))
        self.assertEqual(len(extra), len(plain) + 3)


class FakeWarmProc:
    """Stands in for the hidden commandlet: a thread that speaks the queue protocol of editor/bpkit/warm.py.
    mode: "ok" serves jobs; "die" exits without finishing the first job it takes; "hang" takes a job and never finishes;
    "never_ready" never writes ready.json; "reload_fail" reports a failed reload and does not run the job; "no_end" finishes a
    job without ever logging the END marker; "reload_noise" adds a real error to the reload phase."""

    def __init__(self, args, env, mode="ok"):
        self.pid = 4242
        self.mode = mode
        self.code = None
        self.killed = False
        self.dir = Path(env["ABF_MCP_WARM_DIR"])
        self.log = [a for a in args if a.startswith("-abslog=")][0][len("-abslog="):]
        self.served = []
        self.busy = 0
        self.max_busy = 0
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.thread.join(timeout)
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9

    def exit(self, code=1):
        self.code = code

    def say(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def loop(self):
        if self.mode == "never_ready":
            while self.code is None:
                time.sleep(0.01)
            return
        (self.dir / "ready.json").write_text("{}", encoding="utf-8")
        while self.code is None:
            if (self.dir / "stop.json").exists():
                self.code = 0
                return
            entries = sorted(n for n in os.listdir(str(self.dir / "queue")) if n.endswith(".json"))
            if not entries:
                time.sleep(0.01)
                continue
            path = self.dir / "queue" / entries[0]
            entry = json.loads(path.read_text(encoding="utf-8"))
            job = json.loads(Path(entry["job"]).read_text(encoding="utf-8"))
            self.served.append((entries[0], entry, job))
            if self.mode == "die":
                self.code = 1
                return
            if self.mode == "hang":
                while self.code is None:
                    time.sleep(0.01)
                return
            self.busy += 1
            self.max_busy = max(self.max_busy, self.busy)
            self.say("noise before the job")
            self.say("ABFMCP_WARM_BEGIN " + entry["id"])
            self.say("LogBlueprint: Error: [Compiler] stale class while reloading: Foo is not compatible with Foo")
            if self.mode == "reload_noise":
                self.say("LogBlueprint: Error: [Compiler] Bar doesn't match the property Baz")
                self.say("LogUObjectGlobals: Error: Failed to load package /ModX/Bad/BP_Bad")
            self.say("ABFMCP_WARM_RELOAD {}")
            self.say("LogBlueprint: Error: [Compiler] job %s message" % job["op"])
            time.sleep(0.02)
            run = Path(entry["run"])
            if self.mode == "reload_fail":
                result = {"ok": False, "result": None, "error": "ABFMCP_WARM_RELOAD_FAILED: reload_packages: could not load /ModX/Bad"}
            else:
                result = {"ok": True, "result": {"op": job["op"]}, "error": None}
            (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
            if self.mode != "no_end":
                self.say("ABFMCP_WARM_END " + entry["id"])
            (run / "warm.done").write_text("{}", encoding="utf-8")
            path.unlink()
            self.busy -= 1


class WarmSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="abfwarm"))
        self.stage = SimpleNamespace(name="ModX", dir=self.tmp / "stage", uplugin_path=self.tmp / "stage" / "ModX.uplugin")
        self.stage.dir.mkdir()
        self.procs = []
        self.modes = []
        patches = [mock.patch.object(headless.staging, "set_uplugin"), mock.patch.object(headless, "_exe", return_value="fake.exe"),
                   mock.patch.object(headless, "WARM_SLACK", 0.1), mock.patch.object(headless, "WARM_END_WAIT", 1.0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.addCleanup(headless.WARMS.sessions.clear)

    def spawn(self, args, env):
        mode = self.modes.pop(0) if self.modes else "ok"
        proc = FakeWarmProc(args, env, mode)
        self.procs.append(proc)
        return proc

    def session(self, **kw):
        s = headless.WarmSession(self.stage, spawn=self.spawn, poll=0.01, **kw)
        self.addCleanup(s.stop, 0)
        return s

    def test_queue_protocol_and_log_cut(self):
        s = self.session()
        prepared = []
        res, why = s.submit({"op": "export", "asset_path": "/ModX/A"}, 30, prepared.append)
        self.assertIsNone(why)
        self.assertTrue(res["ok"])
        self.assertTrue(res["warm"])
        self.assertEqual(res["result"], {"op": "export"})
        self.assertEqual(len(prepared), 1)
        name, entry, job = self.procs[0].served[0]
        self.assertTrue(name.startswith("000001-") and name.endswith(".json"))
        self.assertEqual(sorted(entry), ["id", "job", "run"])
        self.assertEqual((job["op"], job["mod"], job["asset_path"]), ("export", "ModX", "/ModX/A"))
        log = Path(res["log"]).read_text(encoding="utf-8")
        self.assertIn("job export message", log)
        self.assertNotIn("noise before the job", log)
        self.assertNotIn("stale class while reloading", log)     # the reload phase is not the job's compile output
        self.assertIn("stale class while reloading", (Path(res["run"]) / "reload.log").read_text(encoding="utf-8"))
        self.assertNotIn("reload_errors", res)                   # stale-class noise only
        lines = log.splitlines()
        self.assertEqual([l.split()[0] for l in (lines[0], lines[1], lines[-1])],
                         ["ABFMCP_WARM_BEGIN", "ABFMCP_WARM_RELOAD", "ABFMCP_WARM_END"])
        parsed = headless.parse_compile_log("".join("[x][0]" + l + "\n" for l in lines))
        self.assertEqual([e["message"] for e in parsed["errors"]], ["job export message"])
        res2, _ = s.submit({"op": "compile", "asset_paths": []}, 30)
        self.assertEqual(len(self.procs), 1)                    # same process
        self.assertTrue(self.procs[0].served[1][0].startswith("000002-"))
        self.assertEqual(s.status()["jobs"], 2)
        self.assertNotIn("noise", Path(res2["log"]).read_text(encoding="utf-8"))
        self.assertTrue(s.stop())
        self.assertFalse(s.alive())

    def test_dead_process_before_a_job_is_restarted(self):
        s = self.session()
        s.submit({"op": "export"}, 30)
        self.procs[0].exit(1)
        res, why = s.submit({"op": "export"}, 30)
        self.assertIsNone(why)
        self.assertTrue(res["warm"])
        self.assertEqual(len(self.procs), 2)

    def test_process_dying_in_a_job_falls_back_to_cold_for_reads(self):
        s = self.session()
        self.modes = ["die"]
        res, why = s.submit({"op": "compile"}, 30)
        self.assertIsNone(res)
        self.assertIn("died", why)
        self.assertFalse(s.alive())

    def test_apply_dying_in_a_job_is_reported_not_rerun(self):
        s = self.session()
        self.modes = ["die"]
        prepared = []
        res, why = s.submit({"op": "apply", "assets": []}, 30, prepared.append)
        self.assertIsNone(why)
        self.assertFalse(res["ok"])
        self.assertTrue(res["warm"])
        self.assertIn("partly written", res["error"])
        self.assertEqual(len(prepared), 1)

    def test_timeout_kills_the_process_and_falls_back(self):
        s = self.session()
        self.modes = ["hang"]
        res, why = s.submit({"op": "reflect"}, 0.2)
        self.assertIsNone(res)
        self.assertIn("exceeded", why)
        self.assertTrue(self.procs[0].killed)

    def test_not_ready_falls_back(self):
        s = self.session(ready_timeout=0.3)
        self.modes = ["never_ready"]
        res, why = s.submit({"op": "export"}, 30)
        self.assertIsNone(res)
        self.assertIn("not ready", why)
        self.assertTrue(self.procs[0].killed)

    def test_jobs_are_serialized(self):
        s = self.session()
        out = []
        threads = [threading.Thread(target=lambda: out.append(s.submit({"op": "compile"}, 30))) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(r[0] and r[0]["warm"] for r in out))
        self.assertEqual(self.procs[0].max_busy, 1)
        self.assertEqual([e[0][:6] for e in self.procs[0].served], ["000001", "000002", "000003", "000004"])

    def test_run_commandlet_uses_warm_then_cold(self):
        headless.WARMS.factory = lambda stage: headless.WarmSession(stage, spawn=self.spawn, poll=0.01)
        self.addCleanup(setattr, headless.WARMS, "factory", headless.WarmSession)
        self.addCleanup(headless.WARMS.stop_all)
        cold = {"ok": True, "result": {"cold": True}, "run": "r", "log": "l", "seconds": 1.0}
        with mock.patch.dict(os.environ, {"ABF_WARM_SESSION": "1"}), mock.patch.object(headless, "_launch", return_value=dict(cold)) as launch:
            res = headless.run_commandlet(self.stage, {"op": "export"}, timeout=30)
            self.assertTrue(res["warm"])
            launch.assert_not_called()
            self.modes = ["die"]
            headless.WARMS.get(self.stage).stop()
            res = headless.run_commandlet(self.stage, {"op": "compile"}, timeout=30)
            self.assertFalse(res["warm"])
            self.assertIn("died", res["warm_fallback"])
            self.assertEqual(res["result"], {"cold": True})
            launch.assert_called_once()

    def test_run_commandlet_stays_cold_without_the_opt_in(self):
        cold = {"ok": True, "result": None, "run": "r", "log": "l", "seconds": 1.0}
        env = dict(os.environ)
        env.pop("ABF_WARM_SESSION", None)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(headless, "_launch", return_value=dict(cold)):
            res = headless.run_commandlet(self.stage, {"op": "export"}, timeout=30)
        self.assertFalse(res["warm"])
        self.assertNotIn("warm_fallback", res)
        self.assertEqual(headless.WARMS.sessions, {})

    def test_game_ops_never_go_warm(self):
        cold = {"ok": True, "result": None, "run": "r", "log": "l", "seconds": 1.0}
        with mock.patch.dict(os.environ, {"ABF_WARM_SESSION": "1"}), mock.patch.object(headless, "_launch", return_value=dict(cold)):
            headless.run_commandlet(self.stage, {"op": "game_test"}, timeout=30)
        self.assertEqual(headless.WARMS.sessions, {})

    def test_cold_launch_stops_a_live_session(self):
        s = self.session()
        s.submit({"op": "export"}, 30)
        headless.WARMS.sessions["ModX"] = s
        self.assertTrue(headless.WARMS.stop_all())
        self.assertFalse(s.alive())

    def test_failed_reload_kills_the_session_and_falls_back_cold(self):
        s = self.session()
        self.modes = ["reload_fail"]
        for op in ("compile", "apply"):                 # the job never ran, so even an apply may run cold
            res, why = s.submit({"op": op}, 30)
            self.assertIsNone(res)
            self.assertIn("warm reload failed", why)
            self.assertIn("could not load /ModX/Bad", why)
            self.assertFalse(s.alive())
            self.modes = ["reload_fail"]
        self.assertTrue(self.procs[0].killed)

    def test_failed_reload_runs_the_job_cold_via_run_commandlet(self):
        headless.WARMS.factory = lambda stage: headless.WarmSession(stage, spawn=self.spawn, poll=0.01)
        self.addCleanup(setattr, headless.WARMS, "factory", headless.WarmSession)
        self.addCleanup(headless.WARMS.stop_all)
        self.modes = ["reload_fail"]
        cold = {"ok": True, "result": {"cold": True}, "run": "r", "log": "l", "seconds": 1.0}
        with mock.patch.dict(os.environ, {"ABF_WARM_SESSION": "1"}), mock.patch.object(headless, "_launch", return_value=dict(cold)):
            res = headless.run_commandlet(self.stage, {"op": "compile"}, timeout=30)
        self.assertFalse(res["warm"])
        self.assertIn("warm reload failed", res["warm_fallback"])
        self.assertEqual(res["result"], {"cold": True})

    def test_reload_errors_that_are_not_stale_class_noise_are_surfaced(self):
        s = self.session()
        self.modes = ["reload_noise"]
        res, why = s.submit({"op": "compile"}, 30)
        self.assertIsNone(why)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["reload_errors"]), 1)
        self.assertIn("Failed to load package /ModX/Bad/BP_Bad", res["reload_errors"][0])
        reload_log = (Path(res["run"]) / "reload.log").read_text(encoding="utf-8")
        self.assertIn("doesn't match the property", reload_log)         # kept for diagnosis, just not reported

    def test_missing_end_marker_is_not_reported_as_done(self):
        s = self.session()
        self.modes = ["no_end"]
        res, why = s.submit({"op": "compile"}, 30)              # a read-only op goes cold: a cut-off log could hide errors
        self.assertIsNone(res)
        self.assertIn("warm log incomplete", why)
        self.assertFalse(s.alive())
        self.modes = ["no_end"]
        res, why = s.submit({"op": "apply", "assets": []}, 30)  # an apply is reported as failed, never rerun
        self.assertIsNone(why)
        self.assertFalse(res["ok"])
        self.assertTrue(res["warm"])
        self.assertIn("warm log incomplete", res["error"])
        self.assertEqual(res["warm_failed"], "warm log incomplete")

    def test_hidden_editor_stops_a_live_warm_session_before_the_run_lock(self):
        import hiddendesk
        s = self.session()
        s.submit({"op": "export"}, 30)
        headless.WARMS.sessions["ModX"] = s
        self.assertTrue(s.alive())
        (self.stage.dir / "ModX.abfmod").write_text("{}", encoding="utf-8")
        seen = []

        class Lock:
            def __enter__(self_):
                seen.append(s.alive())

            def __exit__(self_, *a):
                return False

        with mock.patch.object(headless, "RunLock", Lock), mock.patch.object(headless, "new_run", side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                hiddendesk._run_editor_once(self.stage, {"op": "audit"})
        self.assertEqual(seen, [False])                          # already stopped when the run lock was taken
        self.assertFalse(s.alive())

    def test_warm_session_tool(self):
        tool = [t for t in abfc_tools.build("x") if t[0] == "warm_session"][0][1]
        env = dict(os.environ)
        env.pop("ABF_WARM_SESSION", None)
        with mock.patch.dict(os.environ, env, clear=True):
            text, is_err = tool({"action": "status"})
            self.assertTrue(is_err)
        with mock.patch.dict(os.environ, {"ABF_WARM_SESSION": "1"}):
            text, is_err = tool({"action": "status"})
            self.assertFalse(is_err)
            d = json.loads(text)
            self.assertFalse(d["running"])
            self.assertEqual(sorted(d), ["jobs", "mod", "pid", "running", "sessions", "startedAt"])
            self.assertTrue(tool({"action": "start"})[1])               # needs a mod
            self.assertTrue(tool({"action": "bogus"})[1])
            text, is_err = tool({"action": "stop"})
            self.assertFalse(json.loads(text)["stopped"])

    def test_editor_side_files_compile(self):
        for rel in ("editor/bpkit/warm.py", "editor/warm_loop.py", "editor/bpkit/livepublish.py"):
            py_compile.compile(str(ROOT / rel), cfile=str(self.tmp / "x.pyc"), doraise=True)


class LivePublishEditorSideTests(unittest.TestCase):
    """editor/bpkit/livepublish.py with a stubbed `unreal` module."""

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bpkit.livepublish", None)
        self.addCleanup(sys.modules.pop, "bpkit.livepublish", None)
        import importlib
        self.lp = importlib.import_module("bpkit.livepublish")      # fresh: it binds the stubbed `unreal` at import
        self.u = sys.modules["unreal"]

    def test_prepare_keeps_referenced_packages_loaded(self):
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = None
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.find_package.return_value = mock.MagicMock()
        u.EditorLoadingAndSavingUtils.unload_packages.return_value = (True, "could not unload")
        u.find_object.side_effect = lambda outer, path: mock.MagicMock() if path == "/ModX/A.A" else None
        out = self.lp.prepare({"packages": ["/ModX/A", "/ModX/B"]})
        self.assertEqual(out["refused"], [])
        self.assertEqual(out["kept_loaded"], ["/ModX/A"])
        self.assertEqual(out["unloaded"], ["/ModX/B"])
        # only the editor-closing loop loads assets: nothing is loaded back after unload_packages
        self.assertEqual(u.load_asset.call_count, 2)

    def test_prepare_finds_loaded_referencers_inside_the_mod_only(self):
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = None
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.find_package.return_value = mock.MagicMock()
        u.EditorLoadingAndSavingUtils.unload_packages.return_value = (True, "")
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_referencers.return_value = [
            "/ModX/Ref", "/ModX/A", "/Game/Other", "/ModXOther/Y", "/ModX/Ref"]
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["refused"], [])
        self.assertEqual(out["referencers"], ["/ModX/Ref"])

    def test_prepare_refuses_when_a_referencer_has_unsaved_changes(self):
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = None
        dirty = mock.MagicMock()
        dirty.get_name.return_value = "/ModX/Ref"
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = [dirty]
        u.find_package.return_value = mock.MagicMock()
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_referencers.return_value = ["/ModX/Ref"]
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["refused"], ["/ModX/Ref"])
        self.assertEqual(out["refused_referencers"], ["/ModX/Ref"])
        self.assertIn("unsaved", out["reason"])
        u.EditorLoadingAndSavingUtils.unload_packages.assert_not_called()

    def test_prepare_skips_loaded_maps_as_referencers(self):
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = None
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.find_package.return_value = mock.MagicMock()
        u.EditorLoadingAndSavingUtils.unload_packages.return_value = (True, "")

        def find(outer, path):
            o = mock.MagicMock()
            o.get_class.return_value.get_name.return_value = "World" if path.endswith("MapX.MapX") else "Blueprint"
            return o
        u.find_object.side_effect = find
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_referencers.return_value = ["/ModX/MapX", "/ModX/Ref"]
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["referencers"], ["/ModX/Ref"])
        self.assertEqual(out["skipped_referencers"], ["/ModX/MapX"])

    def _prepare_stubs(self, referencers, dirty=()):
        """Stubbed editor for `prepare`: no PIE, the given dirty package names, everything loaded, and an asset registry whose
        get_referencers answers from the `referencers` dict (package -> list of packages)."""
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = None
        dirty_pkgs = []
        for n in dirty:
            d = mock.MagicMock()
            d.get_name.return_value = n
            dirty_pkgs.append(d)
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = dirty_pkgs
        u.find_package.return_value = mock.MagicMock()
        u.EditorLoadingAndSavingUtils.unload_packages.return_value = (True, "")

        def find(outer, path):
            o = mock.MagicMock()
            o.get_class.return_value.get_name.return_value = "World" if path.rsplit(".", 1)[-1].startswith("Map") else "Blueprint"
            return o
        u.find_object.side_effect = find
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_referencers.side_effect = lambda p, opts: referencers.get(p, [])

    def test_prepare_finds_transitive_referencers_in_breadth_first_order(self):
        self._prepare_stubs({"/ModX/A": ["/ModX/B"], "/ModX/B": ["/ModX/C", "/Game/Other"], "/ModX/C": ["/ModX/A"]})
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["refused"], [])
        self.assertEqual(out["referencers"], ["/ModX/B", "/ModX/C"])

    def test_prepare_refuses_when_a_transitive_referencer_has_unsaved_changes(self):
        self._prepare_stubs({"/ModX/A": ["/ModX/B"], "/ModX/B": ["/ModX/C"]}, dirty=["/ModX/C"])
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["refused"], ["/ModX/C"])
        self.assertEqual(out["refused_referencers"], ["/ModX/C"])
        self.u.EditorLoadingAndSavingUtils.unload_packages.assert_not_called()

    def test_prepare_does_not_expand_a_skipped_map(self):
        self._prepare_stubs({"/ModX/A": ["/ModX/MapX", "/ModX/B"], "/ModX/MapX": ["/ModX/Z"], "/ModX/B": []})
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(out["referencers"], ["/ModX/B"])
        self.assertEqual(out["skipped_referencers"], ["/ModX/MapX"])

    def test_prepare_stops_at_200_referencers(self):
        self._prepare_stubs({"/ModX/A": ["/ModX/R%03d" % i for i in range(250)]})
        out = self.lp.prepare({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertEqual(len(out["referencers"]), 200)
        self.assertEqual(out["referencers"][0], "/ModX/R000")

    def _reload_stubs(self, dependencies):
        """Stubbed editor for `reload_assets`: nothing dirty, every package loads, get_dependencies answers from the
        `dependencies` dict. Returns the list of package names in the order load_asset was called."""
        u = self.u
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.EditorLoadingAndSavingUtils.reload_packages.return_value = (True, "")
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_dependencies.side_effect = lambda p, opts: dependencies.get(p, [])
        names = []

        def load(name):
            names.append(name)
            a = mock.MagicMock()
            a.get_outermost.return_value = name
            return a
        u.load_asset.side_effect = load
        return names

    def test_reload_assets_reloads_dependencies_before_their_dependents(self):
        names = self._reload_stubs({"/ModX/R": ["/ModX/W", "/Game/Base"], "/ModX/W": ["/ModX/A"], "/ModX/A": []})
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/R", "/ModX/A"], "also": ["/ModX/W"]})
        self.assertEqual(names, ["/ModX/A", "/ModX/W", "/ModX/R"])
        self.assertEqual(self.u.EditorLoadingAndSavingUtils.reload_packages.call_args[0][0], ["/ModX/A", "/ModX/W", "/ModX/R"])
        self.assertEqual(out["loaded"], ["/ModX/A", "/ModX/W", "/ModX/R"])
        self.assertEqual(out["reloaded_referencers"], ["/ModX/W"])

    def test_reload_assets_keeps_input_order_on_a_dependency_cycle(self):
        names = self._reload_stubs({"/ModX/A": ["/ModX/B"], "/ModX/B": ["/ModX/A"]})
        self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"], "also": ["/ModX/B"]})
        self.assertEqual(names, ["/ModX/A", "/ModX/B"])

    def test_reload_assets_survives_a_registry_error_in_get_dependencies(self):
        names = self._reload_stubs({})
        self.u.AssetRegistryHelpers.get_asset_registry.return_value.get_dependencies.side_effect = RuntimeError("no registry")
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"], "also": ["/ModX/B"]})
        self.assertEqual(names, ["/ModX/A", "/ModX/B"])
        self.assertEqual(out["reloaded_referencers"], ["/ModX/B"])

    def test_reload_assets_reloads_referencers_after_the_published(self):
        u = self.u
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.EditorLoadingAndSavingUtils.reload_packages.return_value = (True, "")
        names = []

        def load(name):
            names.append(name)
            a = mock.MagicMock()
            a.get_outermost.return_value = name
            return a
        u.load_asset.side_effect = load
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"], "also": ["/ModX/Ref", "/ModX/A"]})
        self.assertEqual(names, ["/ModX/A", "/ModX/Ref"])
        self.assertEqual(out["reloaded_referencers"], ["/ModX/Ref"])
        self.assertEqual(u.EditorLoadingAndSavingUtils.reload_packages.call_args[0][0], ["/ModX/A", "/ModX/Ref"])

    def test_reload_assets_skips_dirty_referencers_and_reports_dirty_published(self):
        u = self.u
        pa, pdirty = mock.MagicMock(), mock.MagicMock()
        pa.get_name.return_value = "/ModX/A"
        pdirty.get_name.return_value = "/ModX/Dirty"
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = [pa, pdirty]
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.EditorLoadingAndSavingUtils.reload_packages.return_value = (True, "")
        names = []

        def load(name):
            names.append(name)
            a = mock.MagicMock()
            a.get_outermost.return_value = name
            return a
        u.load_asset.side_effect = load
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"], "also": ["/ModX/Dirty", "/ModX/Ref"]})
        self.assertEqual(names, ["/ModX/A", "/ModX/Ref"])
        self.assertEqual(u.EditorLoadingAndSavingUtils.reload_packages.call_args[0][0], ["/ModX/A", "/ModX/Ref"])
        self.assertEqual(out["skipped_dirty"], ["/ModX/Dirty"])
        self.assertEqual(out["dirty_published"], ["/ModX/A"])
        self.assertEqual(out["reloaded_referencers"], ["/ModX/Ref"])

    def test_recompile_compiles_blueprints_dependencies_first_and_saves_nothing(self):
        u = self.u

        class FakeBlueprint:
            pass
        u.Blueprint = FakeBlueprint
        u.find_object.side_effect = lambda outer, path: FakeBlueprint() if "Data" not in path else object()
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = []
        u.AssetRegistryHelpers.get_asset_registry.return_value.get_dependencies.side_effect = (
            lambda p, opts: {"/ModX/R": ["/ModX/W"], "/ModX/W": []}.get(p, []))
        out = self.lp.recompile({"root": "/ModX", "packages": ["/ModX/R", "/ModX/Data", "/ModX/W"]})
        self.assertEqual(out["compiled"], ["/ModX/W", "/ModX/R"])
        self.assertEqual(out["skipped"], ["/ModX/Data"])
        self.assertEqual(u.BlueprintEditorLibrary.compile_blueprint.call_count, 2)
        u.EditorAssetLibrary.save_loaded_asset.assert_not_called()

    def test_prepare_refuses_pie_without_unloading(self):
        u = self.u
        u.get_editor_subsystem.return_value.get_game_world.return_value = object()
        out = self.lp.prepare({"packages": ["/ModX/A"]})
        self.assertEqual(out["refused"], ["/ModX/A"])
        self.assertEqual(out["reason"], "Play In Editor is running")
        u.EditorLoadingAndSavingUtils.unload_packages.assert_not_called()

    def test_reload_assets_reports_error_and_dirty(self):
        u = self.u
        a, z = mock.MagicMock(), mock.MagicMock()
        a.get_name.return_value = "/ModX/A"
        z.get_name.return_value = "/Other/Z"
        u.EditorLoadingAndSavingUtils.get_dirty_content_packages.return_value = [a, z]
        u.find_object.side_effect = lambda outer, path: mock.MagicMock()
        u.EditorLoadingAndSavingUtils.reload_packages.return_value = (True, "boom")
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertIn("boom", out["error"])
        self.assertEqual(out["dirty_after"], ["/ModX/A"])
        self.assertEqual(out["missing"], [])
        u.EditorLoadingAndSavingUtils.reload_packages.return_value = (True, "")
        out = self.lp.reload_assets({"root": "/ModX", "packages": ["/ModX/A"]})
        self.assertNotIn("error", out)


class WarmEditorSideTests(unittest.TestCase):
    """editor/bpkit/warm.py with a stubbed `unreal` module: the reload result must be checked before the job runs."""

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)
        from bpkit import warm
        self.warm = warm
        self.tmp = Path(tempfile.mkdtemp(prefix="abfwarmed"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_call_problem_reads_bool_and_tuple_results(self):
        p = self.warm.call_problem
        self.assertIsNone(p("reload_packages", True))
        self.assertIsNone(p("reload_packages", (True, "")))
        self.assertIsNone(p("reload_packages", (False, True)))      # flags alone are not judged
        self.assertIn("returned False", p("reload_packages", False))
        self.assertIn("boom", p("reload_packages", (True, "boom")))
        self.assertIn("boom", p("unload_packages", [False, "boom"]))

    def entry(self, op="export"):
        run = self.tmp / "run"
        run.mkdir(exist_ok=True)
        job = self.tmp / "job.json"
        job.write_text(json.dumps({"op": op, "mod": "ModX"}), encoding="utf-8")
        return {"id": "j1", "job": str(job), "run": str(run)}, run

    def test_failed_reload_fails_the_job_before_it_runs(self):
        entry, run = self.entry()
        ran = []
        report = {"reloaded": 2, "unloaded": 0, "problems": ["reload_packages: boom"]}
        with mock.patch.object(self.warm, "reload_mod", return_value=report), \
                mock.patch.object(self.warm.core, "configure"), \
                mock.patch.dict(self.warm.jobs.COMMANDLET_OPS, {"export": lambda job: ran.append(job) or {}}):
            self.warm.run_entry(entry, str(self.tmp))
        res = json.loads((run / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"].startswith(self.warm.RELOAD_FAILED))
        self.assertIn("boom", res["error"])
        self.assertEqual(ran, [])
        self.assertTrue((run / "warm.done").is_file())

    def test_raising_reload_fails_the_job_the_same_way(self):
        entry, run = self.entry()
        with mock.patch.object(self.warm, "reload_mod", side_effect=RuntimeError("no editor")), \
                mock.patch.object(self.warm.core, "configure"):
            self.warm.run_entry(entry, str(self.tmp))
        res = json.loads((run / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(res["error"].startswith(self.warm.RELOAD_FAILED))
        self.assertIn("no editor", res["error"])

    def test_clean_reload_runs_the_job(self):
        entry, run = self.entry()
        with mock.patch.object(self.warm, "reload_mod", return_value={"reloaded": 1, "unloaded": 0}), \
                mock.patch.object(self.warm.core, "configure"), \
                mock.patch.dict(self.warm.jobs.COMMANDLET_OPS, {"export": lambda job: {"done": True}}):
            self.warm.run_entry(entry, str(self.tmp))
        res = json.loads((run / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((res["ok"], res["result"]), (True, {"done": True}))


class BackendInfoCapabilityTests(unittest.TestCase):
    def capabilities(self, warm, abfc_tools=False):
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "backend_info", "arguments": {}}}]
        env = dict(os.environ, ABF_HEADLESS_DIR=_TMP)
        env.pop("ABF_WARM_SESSION", None)
        env.pop("ABF_ABFC_TOOLS", None)
        if warm:
            env["ABF_WARM_SESSION"] = "1"
        if abfc_tools:
            env["ABF_ABFC_TOOLS"] = "1"
        p = subprocess.run([sys.executable, "-I", str(ROOT / "server.py")], input="\n".join(json.dumps(r) for r in reqs) + "\n",
                           capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
        replies = {json.loads(l)["id"]: json.loads(l) for l in p.stdout.splitlines() if l.strip()}
        return json.loads(replies[2]["result"]["content"][0]["text"])["capabilities"]

    def test_warm_session_flag_follows_the_env(self):
        self.assertIs(self.capabilities(False)["warmSession"], False)
        caps = self.capabilities(True)
        self.assertIs(caps["warmSession"], True)
        self.assertIs(caps["pinTypes"], True)

    def test_reflect_flag_follows_abfc_tools(self):
        self.assertIs(self.capabilities(False)["reflect"], False)
        self.assertIs(self.capabilities(False, abfc_tools=True)["reflect"], True)

    def test_fixed_capabilities(self):
        caps = self.capabilities(False)
        self.assertIs(caps["gameplayTags"], True)
        self.assertIs(caps["reflectDefaults"], True)
        self.assertEqual(caps["assetOps"], ["rename", "delete", "reparent", "recreate"])
        self.assertIs(caps["compileErrorNodeRefs"], True)
        self.assertIs(caps["functionLocals"], False)
        self.assertIs(caps["userPureFunctions"], False)
        self.assertIn("UserDefinedStruct", caps["createKinds"])
        for k in ("gameTest", "audit", "publish"):
            self.assertIs(caps[k], True)


class EngineVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="abfengver"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        (self.tmp / "Engine" / "Build").mkdir(parents=True)

    def test_reads_build_version(self):
        (self.tmp / "Engine" / "Build" / "Build.version").write_text(
            '{"MajorVersion":5,"MinorVersion":4,"PatchVersion":4}', encoding="utf-8")
        self.assertEqual(paths.engine_version(self.tmp), "5.4.4")

    def test_none_for_missing_root_file_or_bad_json(self):
        self.assertIsNone(paths.engine_version(None))
        self.assertIsNone(paths.engine_version(self.tmp))
        (self.tmp / "Engine" / "Build" / "Build.version").write_text("{not json", encoding="utf-8")
        self.assertIsNone(paths.engine_version(self.tmp))
        (self.tmp / "Engine" / "Build" / "Build.version").write_text('{"MajorVersion":5}', encoding="utf-8")
        self.assertIsNone(paths.engine_version(self.tmp))


class ReflectMetaSwitchTests(unittest.TestCase):
    """abfc_tools.reflect: the `meta` job key comes from the argument, else from ABF_REFLECT_META."""

    def run_reflect(self, args, env=None):
        jobs = []

        def fake(stage, job, timeout=None):
            jobs.append(job)
            return {"ok": True, "run": str(_TMP), "seconds": 1, "result": {"counts": {}, "meta": "skipped"}}

        reflect = abfc_tools.build("x")[1][1]
        env_patch = {"ABF_REFLECT_META": env} if env is not None else {}
        with mock.patch.dict(os.environ, env_patch), \
                mock.patch.object(abfc_tools.headless, "run_commandlet", side_effect=fake), \
                mock.patch.object(abfc_tools.staging, "load", return_value=object()), \
                mock.patch.object(abfc_tools.staging, "strip_scratch"), \
                mock.patch.object(abfc_tools.bpscan, "load_filter", return_value=({}, {})):
            if env is None:
                os.environ.pop("ABF_REFLECT_META", None)
            text, is_err = reflect(dict({"mod": "Mod", "classes": ["/Script/Engine.Actor"]}, **args))
        self.assertFalse(is_err, text)
        return jobs[0], json.loads(text)

    def test_default_reads_meta(self):
        job, out = self.run_reflect({})
        self.assertIs(job["meta"], True)
        self.assertEqual(out["meta"], "skipped")     # echoes the editor-side result

    def test_argument_false_skips(self):
        self.assertIs(self.run_reflect({"meta": False})[0]["meta"], False)

    def test_env_zero_skips_without_argument(self):
        self.assertIs(self.run_reflect({}, env="0")[0]["meta"], False)

    def test_env_other_value_reads(self):
        self.assertIs(self.run_reflect({}, env="1")[0]["meta"], True)

    def test_argument_true_wins_over_env(self):
        self.assertIs(self.run_reflect({"meta": True}, env="0")[0]["meta"], True)


class ReflectRunOrderTests(unittest.TestCase):
    """editor/bpkit/reflect.py with a stubbed `unreal`: function metadata is read after the template runs and can be skipped."""

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)
        from bpkit import reflect
        self.reflect = reflect
        self.tmp = Path(tempfile.mkdtemp(prefix="abfreflect"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def run_job(self, extra):
        calls = []
        r = self.reflect
        r.unreal.load_class.return_value = mock.MagicMock()

        def templates(items, errors, what):
            calls.append(("templates", what))
            return {}

        def collect(get_tag, items, **kw):
            calls.append(("meta",))
            return {}

        with mock.patch.object(r, "_templates", side_effect=templates), \
                mock.patch.object(r.rp, "collect_function_meta", side_effect=collect), \
                mock.patch.object(r, "_function_index", return_value={}), \
                mock.patch.object(r, "_ancestors", return_value=[]):
            res = r.run(dict({"run": str(self.tmp), "classes": ["/Script/Engine.Actor"]}, **extra))
        written = json.loads((self.tmp / "reflect.json").read_text(encoding="utf-8"))
        return calls, res, written

    def test_meta_is_read_after_the_function_templates(self):
        calls, res, written = self.run_job({})
        self.assertIn(("meta",), calls)
        self.assertGreater(calls.index(("meta",)), calls.index(("templates", "function")))
        self.assertEqual(calls.index(("meta",)), len(calls) - 1)    # after every template run
        self.assertEqual((res["meta"], written["meta"]), ("read", "read"))

    def test_meta_false_skips_the_read(self):
        calls, res, written = self.run_job({"meta": False})
        self.assertNotIn(("meta",), calls)
        self.assertIn(("templates", "function"), calls)
        self.assertEqual((res["meta"], written["meta"]), ("skipped", "skipped"))


NODE_T3D = (
    'Begin Object Class=/Script/Engine.Blueprint Name="BP_A" ExportPath="/Script/Engine.Blueprint\'/Mod/X/BP_A.BP_A\'"\n'
    '   Begin Object Class=/Script/Engine.EdGraph Name="EventGraph" ExportPath="x"\n'
    '      Begin Object Class=/Script/BlueprintGraph.K2Node_CallFunction Name="K2Node_CallFunction_12" ExportPath="x"\n'
    '      End Object\n'
    '      Begin Object Class=/Script/BlueprintGraph.K2Node_CallFunction Name="K2Node_CallFunction_13" ExportPath="x"\n'
    '      End Object\n'
    '      Begin Object Class=/Script/BlueprintGraph.K2Node_VariableSet Name="K2Node_VariableSet_1" ExportPath="x"\n'
    '      End Object\n'
    '      Begin Object Name="K2Node_CallFunction_12" ExportPath="x"\n'
    '         NodePosX=0\n'
    '         NodePosY=0\n'
    '         bHasCompilerMessage=True\n'
    '         ErrorMsg="Bad   pin: Integer is not compatible with Name."\n'
    '      End Object\n'
    '      Begin Object Name="K2Node_CallFunction_13" ExportPath="x"\n'
    '         NodePosX=0\n'
    '         NodePosY=0\n'
    '         ErrorMsg="Bad pin: Integer is not compatible with Name."\n'
    '      End Object\n'
    '      Begin Object Name="K2Node_VariableSet_1" ExportPath="x"\n'
    '         NodePosX=0\n'
    '         NodePosY=0\n'
    '      End Object\n'
    '   End Object\n'
    '   Begin Object Class=/Script/Engine.EdGraph Name="UpdateTarget" ExportPath="x"\n'
    '      Begin Object Class=/Script/BlueprintGraph.K2Node_IfThenElse Name="K2Node_IfThenElse_4" ExportPath="x"\n'
    '      End Object\n'
    '      Begin Object Name="K2Node_IfThenElse_4" ExportPath="x"\n'
    '         NodePosX=0\n'
    '         NodePosY=0\n'
    '         ErrorMsg="Condition pin is not connected to anything useful at all"\n'
    '      End Object\n'
    '   End Object\n'
    'End Object\n')


class NodeRefTests(unittest.TestCase):
    def test_compiler_node_errors_lists_only_nodes_with_messages(self):
        import bpscan
        got = bpscan.compiler_node_errors(NODE_T3D)
        self.assertEqual([(g, n) for g, n, _ in got],
                         [("EventGraph", "K2Node_CallFunction_12"), ("EventGraph", "K2Node_CallFunction_13"), ("UpdateTarget", "K2Node_IfThenElse_4")])

    def test_attach_matches_equal_text_once_per_node_in_export_order(self):
        import bpscan
        nodes = {"/Mod/X/BP_A": bpscan.compiler_node_errors(NODE_T3D)}
        entries = [{"asset": "/Mod/X/BP_A", "message": "Bad pin: Integer is not compatible with Name."},
                   {"asset": "/Mod/X/BP_A", "message": "Bad pin: Integer is not compatible with Name."},
                   {"asset": "/Mod/X/BP_A", "message": "Bad pin: Integer is not compatible with Name."},
                   {"asset": "/Mod/X/BP_A", "message": "Condition pin is not connected"},
                   {"asset": "/Mod/X/BP_A", "message": "Unrelated text from nowhere"},
                   {"asset": "/Mod/X/Other", "message": "Bad pin: Integer is not compatible with Name."},
                   {"asset": "/Mod/X/BP_A", "message": "Kept", "node": "K2Node_Keep", "graph": "G"}]
        n = headless.attach_node_refs(entries, nodes)
        self.assertEqual(n, 3)
        self.assertEqual([e.get("node") for e in entries],
                         ["K2Node_CallFunction_12", "K2Node_CallFunction_13", None, "K2Node_IfThenElse_4", None, None, "K2Node_Keep"])
        self.assertEqual(entries[3]["graph"], "UpdateTarget")
        self.assertEqual(entries[0]["node_from"], "export")
        self.assertEqual(entries[4], {"asset": "/Mod/X/BP_A", "message": "Unrelated text from nowhere"})

    # ErrorMsg lines copied from a real export (apply run with an Equal node whose pins were forced to Integer):
    # apostrophes are written \', messages of one node are joined by a literal backslash-n.
    REAL_EQUAL = (r'Can\'t connect pins  A  and  Abfc Update Target T 0 : Integer is not compatible with Name.\n'
                  r'Can\'t connect pins  B  and  Last Row Name : Integer is not compatible with Name.\n'
                  r'Can\'t connect pins  Last Row Name  and  B : Name is not compatible with Integer.\n'
                  r'Can\'t connect pins  Abfc Update Target T 0  and  A : Name is not compatible with Integer.')
    REAL_GET_B = (r'Can\'t connect pins  B  and  Last Row Name : Integer is not compatible with Name.\n'
                  r'Can\'t connect pins  Last Row Name  and  B : Name is not compatible with Integer.')
    REAL_GET_A = (r'Can\'t connect pins  A  and  Abfc Update Target T 0 : Integer is not compatible with Name.\n'
                  r'Can\'t connect pins  Abfc Update Target T 0  and  A : Name is not compatible with Integer.')
    REAL_CAST = r'\'Item\' is already a \'W Player HUD Interaction Prompt\', you don\'t need  Cast To W_PlayerHUD_InteractionPrompt_C .'

    @classmethod
    def real_t3d(cls):
        def node(cls_, name, msg):
            return ('      Begin Object Class=/Script/BlueprintGraph.%s Name="%s" ExportPath="x"\n      End Object\n' % (cls_, name),
                    '      Begin Object Name="%s" ExportPath="x"\n         NodePosX=0\n         NodePosY=0\n'
                    '         ErrorMsg="%s"\n      End Object\n' % (name, msg))
        ns = [node("K2Node_CallFunction", "K2Node_CallFunction_9036", cls.REAL_EQUAL),
              node("K2Node_VariableGet", "K2Node_VariableGet_9035", cls.REAL_GET_B),
              node("K2Node_VariableGet", "K2Node_VariableGet_9034", cls.REAL_GET_A),
              node("K2Node_DynamicCast", "K2Node_DynamicCast_9004", cls.REAL_CAST)]
        return ('Begin Object Class=/Script/Engine.Blueprint Name="BP_A" ExportPath="/Script/Engine.Blueprint\'/Mod/X/BP_A.BP_A\'"\n'
                '   Begin Object Class=/Script/Engine.EdGraph Name="EventGraph" ExportPath="x"\n'
                + "".join(d for d, _ in ns) + "".join(b for _, b in ns) + '   End Object\nEnd Object\n')

    def test_unescape_t3d_handles_every_escape_ue_writes(self):
        import bpscan
        self.assertEqual(bpscan.unescape_t3d(r"Can\'t a\nb\tc\rd \"q\" back\\slash \\n end"),
                         "Can't a\nb\tc\rd \"q\" back\\slash \\n end")
        self.assertEqual(bpscan.unescape_t3d(""), "")
        self.assertEqual(bpscan.unescape_t3d(None), "")

    def test_real_escaped_messages_split_into_one_entry_per_message(self):
        import bpscan
        got = bpscan.compiler_node_errors(self.real_t3d())
        self.assertEqual([(n, m) for _, n, m in got if n == "K2Node_CallFunction_9036"],
                         [("K2Node_CallFunction_9036", "Can't connect pins  A  and  Abfc Update Target T 0 : Integer is not compatible with Name."),
                          ("K2Node_CallFunction_9036", "Can't connect pins  B  and  Last Row Name : Integer is not compatible with Name."),
                          ("K2Node_CallFunction_9036", "Can't connect pins  Last Row Name  and  B : Name is not compatible with Integer."),
                          ("K2Node_CallFunction_9036", "Can't connect pins  Abfc Update Target T 0  and  A : Name is not compatible with Integer.")])
        self.assertEqual(got[-1][2], "'Item' is already a 'W Player HUD Interaction Prompt', you don't need  Cast To W_PlayerHUD_InteractionPrompt_C .")
        self.assertTrue(all("\\" not in m for _, _, m in got))

    def test_attach_matches_log_errors_that_have_apostrophes_and_newlines(self):
        import bpscan
        nodes = {"/Mod/X/BP_A": bpscan.compiler_node_errors(self.real_t3d())}
        log = ["Can't connect pins  A  and  Abfc Update Target T 0 : Integer is not compatible with Name.",
               "Can't connect pins  B  and  Last Row Name : Integer is not compatible with Name.",
               "Can't connect pins  Last Row Name  and  B : Name is not compatible with Integer.",
               "Can't connect pins  Abfc Update Target T 0  and  A : Name is not compatible with Integer.",
               "'Item' is already a 'W Player HUD Interaction Prompt', you don't need  Cast To W_PlayerHUD_InteractionPrompt_C ."]
        entries = [{"asset": "/Mod/X/BP_A", "message": m} for m in log]
        self.assertEqual(headless.attach_node_refs(entries, nodes), 5)
        self.assertEqual([e["node"] for e in entries], ["K2Node_CallFunction_9036"] * 4 + ["K2Node_DynamicCast_9004"])
        self.assertTrue(all(e["graph"] == "EventGraph" and e["node_from"] == "export" for e in entries))

    def test_compile_report_carries_graph_and_node(self):
        import bpscan  # noqa: F401
        import headless_tools
        run = Path(tempfile.mkdtemp(prefix="abfnode"))
        try:
            (run / "compile_0.t3d").write_text(NODE_T3D, encoding="utf-8")
            log = run / "editor.log"
            log.write_text("[x][0]LogBlueprint: Error: [Compiler] Bad pin: Integer is not compatible with Name. from Source: /Mod/X/BP_A.BP_A\n"
                           "[x][0]LogBlueprint: Warning: [Compiler] Condition pin is not connected to anything useful at all from Source: /Mod/X/BP_A.BP_A\n",
                           encoding="utf-8")
            res = {"ok": True, "run": str(run), "log": str(log), "seconds": 1.0, "warm": True,
                   "result": {"compiled": ["/Mod/X/BP_A"], "skipped": [], "node_files": {"/Mod/X/BP_A": "compile_0.t3d"}}}
            tools = {t[0]: t[1] for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                                ROOT / "editor", [], "", list_processes=lambda: [])}
            with mock.patch.object(headless.staging, "load", return_value=object()), \
                    mock.patch.object(headless, "run_commandlet", return_value=res) as rc:
                text, is_err = tools["bp_compile_report"]({"mod": "Mod", "asset_paths": ["/Mod/X/BP_A"]})
            self.assertEqual(rc.call_args[0][1]["node_refs"], True)
            self.assertFalse(is_err)
            d = json.loads(text)
            self.assertEqual([(m["severity"], m["graph"], m["node"]) for m in d["messages"]],
                             [("error", "EventGraph", "K2Node_CallFunction_12"), ("warning", "UpdateTarget", "K2Node_IfThenElse_4")])
            self.assertIs(d["warm"], True)
        finally:
            shutil.rmtree(str(run), ignore_errors=True)

    def test_compile_report_without_a_matching_node_leaves_messages_unchanged(self):
        import headless_tools
        run = Path(tempfile.mkdtemp(prefix="abfnode"))
        try:
            log = run / "editor.log"
            log.write_text("[x][0]LogBlueprint: Error: [Compiler] Some error from Source: /Mod/X/BP_A.BP_A\n", encoding="utf-8")
            res = {"ok": True, "run": str(run), "log": str(log), "seconds": 1.0,
                   "result": {"compiled": ["/Mod/X/BP_A"], "skipped": []}}
            tools = {t[0]: t[1] for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                                ROOT / "editor", [], "", list_processes=lambda: [])}
            with mock.patch.object(headless.staging, "load", return_value=object()), \
                    mock.patch.object(headless, "run_commandlet", return_value=res):
                text, _ = tools["bp_compile_report"]({"mod": "Mod", "node_refs": False})
            m = json.loads(text)["messages"][0]
            self.assertEqual((m["graph"], m["node"]), (None, None))
        finally:
            shutil.rmtree(str(run), ignore_errors=True)

    def test_failed_apply_restores_the_stage_bytes_and_names_the_node(self):
        """A bp_apply whose compile log has errors (the commandlet itself says ok and saved) is rolled back byte for byte,
        also when the run was warm, and its errors carry the node found in the escaped export."""
        import headless_tools
        run = Path(tempfile.mkdtemp(prefix="abfroll"))
        stage_dir = Path(tempfile.mkdtemp(prefix="abfstage"))
        try:
            asset = stage_dir / "Content" / "X" / "BP_A.uasset"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"GOOD-BYTES")
            stage = SimpleNamespace(dir=stage_dir, name="Mod")
            log = run / "editor.log"
            log.write_text("[x][0]LogBlueprint: Error: [Compiler] Can't connect pins  A  and  Abfc Update Target T 0 : "
                           "Integer is not compatible with Name. from Source: /Mod/X/BP_A.BP_A\n", encoding="utf-8")
            (run / "final_0.t3d").write_text(self.real_t3d(), encoding="utf-8")

            def fake_run(stage_, job, timeout=300, prepare=None):
                prepare(run)                                  # the snapshot, as in the warm and the cold path
                asset.write_bytes(b"BROKEN-BYTES-SAVED-BY-THE-EDITOR")
                (stage_dir / "Content" / "X" / "BP_A.uexp").write_bytes(b"NEW")
                return {"ok": True, "run": str(run), "log": str(log), "seconds": 1.0, "warm": True,
                        "result": {"assets": [{"asset": "/Mod/X/BP_A", "node_ids": {}, "final": "final_0.t3d"}], "saved": 1}}

            tools = {t[0]: t[1] for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                                ROOT / "editor", [], "", list_processes=lambda: [])}
            with mock.patch.object(headless_tools.staging, "load", return_value=stage), \
                    mock.patch.object(headless_tools.staging, "strip_scratch"), \
                    mock.patch.object(headless, "run_commandlet", side_effect=fake_run):
                text, is_err = tools["bp_apply"]({"mod": "Mod", "assets": [{"asset": "/Mod/X/BP_A"}]})
            d = json.loads(text)
            self.assertTrue(is_err)
            self.assertIs(d["ok"], False)
            self.assertIs(d["rolled_back"], True)
            self.assertEqual(asset.read_bytes(), b"GOOD-BYTES")
            self.assertFalse((stage_dir / "Content" / "X" / "BP_A.uexp").exists())
            e = d["compile"]["errors"][0]
            self.assertEqual((e["graph"], e["node"], e["node_from"]), ("EventGraph", "K2Node_CallFunction_9036", "export"))
            self.assertIn("Can't connect pins", e["message"])
        finally:
            shutil.rmtree(str(run), ignore_errors=True)
            shutil.rmtree(str(stage_dir), ignore_errors=True)


class ApplyTagsIniHostTests(unittest.TestCase):
    """bp_apply with a class_manager_row gameplay_tag: the stage's tag ini is written before the commandlet runs and
    restored when the apply fails."""
    ROW = {"asset_path": "/Mod/Tables/Row_A", "class_path": "/Mod/X/BP_A.BP_A_C", "gameplay_tag": "ClassManager.EntityDirector.A"}

    def run_apply(self, ok, old=None):
        import headless_tools
        import tagsini
        run = Path(tempfile.mkdtemp(prefix="abftagrun"))
        stage_dir = Path(tempfile.mkdtemp(prefix="abftagstage"))
        self.addCleanup(shutil.rmtree, str(run), True)
        self.addCleanup(shutil.rmtree, str(stage_dir), True)
        ini = tagsini.ini_path(stage_dir, "Mod")
        if old is not None:
            ini.parent.mkdir(parents=True)
            ini.write_bytes(old)
        log = run / "editor.log"
        log.write_text("", encoding="utf-8")
        seen = {}

        def fake_run(stage_, job, timeout=300, prepare=None):
            prepare(run)
            seen["ini"] = ini.read_bytes() if ini.is_file() else None
            return {"ok": ok, "run": str(run), "log": str(log), "seconds": 1.0, "error": None if ok else "boom",
                    "result": {"assets": [], "rows": [], "saved": 0}}

        tools = {t[0]: t[1] for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                            ROOT / "editor", [], "", list_processes=lambda: [])}
        stage = SimpleNamespace(dir=stage_dir, name="Mod")
        with mock.patch.object(headless_tools.staging, "load", return_value=stage), \
                mock.patch.object(headless_tools.staging, "strip_scratch"), \
                mock.patch.object(headless, "run_commandlet", side_effect=fake_run):
            text, _ = tools["bp_apply"]({"mod": "Mod", "assets": [{"asset": "/Mod/X/BP_A", "class_manager_row": dict(self.ROW)}]})
        return json.loads(text), seen, ini

    def test_tag_is_in_the_ini_before_the_run(self):
        d, seen, ini = self.run_apply(True)
        self.assertIn(b'GameplayTagList=(Tag="ClassManager.EntityDirector.A",DevComment="")', seen["ini"])
        self.assertEqual(d["tags_added"], ["ClassManager.EntityDirector.A"])
        self.assertTrue(ini.is_file())

    def test_known_tag_adds_nothing(self):
        old = b'[/Script/GameplayTags.GameplayTagsList]\r\nGameplayTagList=(Tag="ClassManager.EntityDirector.A",DevComment="")\r\n'
        d, seen, ini = self.run_apply(True, old)
        self.assertEqual(d["tags_added"], [])
        self.assertEqual(ini.read_bytes(), old)

    def test_failed_apply_restores_a_missing_ini(self):
        d, seen, ini = self.run_apply(False)
        self.assertIsNotNone(seen["ini"])
        self.assertFalse(ini.exists())
        self.assertEqual(d["tags_added"], [])

    def test_failed_apply_restores_the_old_ini_bytes(self):
        old = b'[/Script/GameplayTags.GameplayTagsList]\r\nGameplayTagList=(Tag="Other.Tag",DevComment="")\r\n'
        d, seen, ini = self.run_apply(False, old)
        self.assertNotEqual(seen["ini"], old)
        self.assertEqual(ini.read_bytes(), old)


REG_LOG = (
    "[2026.11.05-20.32.58:042][ 35]LogRowRegistry: Registered mod plugin 'OtherModAbc12' (row prefix 'ROWPFX') as a row source.\n"
    "[2026.11.05-20.33.03:072][ 35]AbioticFactorEditorCookedLog: Display: Registered 0 'PlayerCharacterVoiceDataAsset' primary assets from mod 'OtherModAbc12'\n"
    "[2026.11.05-20.33.04:042][ 35]LogRowRegistry: Registered mod plugin 'TestModXyz34' (row prefix 'ROWPF2') as a row source.\n")


class StageAuditPickerTests(unittest.TestCase):
    def setUp(self):
        import hiddendesk
        self.hd = hiddendesk

    def test_registered_mods_reads_the_registry_lines_only(self):
        self.assertEqual(self.hd.registered_mods(REG_LOG), ["OtherModAbc12", "TestModXyz34"])
        self.assertEqual(self.hd.registered_mods(""), [])
        self.assertEqual(self.hd.registered_mods(None), [])

    def test_wrong_mount_names_the_other_mod(self):
        self.assertEqual(self.hd.wrong_mount(REG_LOG, "TestModXyz34"), "OtherModAbc12")
        only = "LogRowRegistry: Registered mod plugin 'TestModXyz34' (row prefix 'X') as a row source.\n"
        self.assertIsNone(self.hd.wrong_mount(only, "TestModXyz34"))
        self.assertIsNone(self.hd.wrong_mount("", "TestModXyz34"))

    def test_login_stuck_needs_a_login_window_and_no_mod_browser(self):
        ls = self.hd.login_stuck
        self.assertEqual(ls([], None, 100.0, 60), (None, False))
        self.assertEqual(ls(["Unreal Editor"], None, 100.0, 60), (None, False))
        self.assertEqual(ls(["Unreal Editor"], 50.0, 100.0, 60), (None, False))      # state ended: the clock restarts

    def test_login_stuck_first_sighting_then_grace(self):
        ls = self.hd.login_stuck
        self.assertEqual(ls(["Logging In"], None, 100.0, 60), (100.0, False))
        self.assertEqual(ls(["Logging In"], 100.0, 159.0, 60), (100.0, False))
        self.assertEqual(ls(["Logging In"], 100.0, 160.0, 60), (100.0, True))
        self.assertEqual(ls(["Logging In"], 100.0, 500.0, 60), (100.0, True))
        self.assertEqual(ls(["Logging In"], None, 100.0, 0), (100.0, True))

    def test_login_stuck_resets_when_the_mod_browser_shows(self):
        ls = self.hd.login_stuck
        self.assertEqual(ls(["Logging In", "Mod Browser"], 100.0, 500.0, 60), (None, False))

    def test_login_stuck_is_case_insensitive_and_ignores_other_titles(self):
        ls = self.hd.login_stuck
        self.assertEqual(ls(["", "Epic Games - LOGGING IN...", "AbioticFactor"], 10.0, 100.0, 60), (10.0, True))
        self.assertEqual(ls(["", "mod browser", "logging in"], 10.0, 100.0, 60), (None, False))
        self.assertEqual(ls(["", None, "x"], None, 5.0, 60), (None, False))

    def test_redirect_home_points_userprofile_at_a_private_documents_folder(self):
        with mock.patch.dict(os.environ, {"ABF_PICKER_HOME": "1"}):
            env = self.hd.redirect_home({"USERPROFILE": "C:\\real", "LOCALAPPDATA": "x"})
        home = Path(env["USERPROFILE"])
        self.assertEqual(home, headless.root() / "home")
        self.assertTrue((home / "Documents" / "Abiotic Factor Mods").is_dir())
        self.assertEqual(os.listdir(str(home / "Documents" / "Abiotic Factor Mods")), [])
        self.assertEqual(env["LOCALAPPDATA"], "x")

    def test_redirect_home_links_appdata_to_the_real_one(self):
        real = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(real), True)
        calls = []
        with mock.patch.dict(os.environ, {"ABF_PICKER_HOME": "1"}),                 mock.patch.object(self.hd.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            self.hd.redirect_home({"USERPROFILE": "C:\\real", "LOCALAPPDATA": str(real), "APPDATA": str(real)})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:4], ["cmd", "/c", "mklink", "/J"])
        self.assertTrue(calls[0][4].endswith(os.path.join("AppData", "Local")))
        self.assertTrue(calls[1][4].endswith(os.path.join("AppData", "Roaming")))
        self.assertEqual(calls[0][5], str(real))

    def test_picker_candidates_try_the_private_list_tile_first_unless_redirect_is_off(self):
        win = (1, "Abiotic Factor Mod Browser", 532, 151, 1495, 1078)
        with mock.patch.dict(os.environ, {"ABF_PICKER_HOME": "1"}):
            os.environ.pop("ABF_PICKER_TILE", None)
            on = self.hd._picker_candidates(win)
        with mock.patch.dict(os.environ, {"ABF_PICKER_HOME": "0"}):
            os.environ.pop("ABF_PICKER_TILE", None)
            off = self.hd._picker_candidates(win)
        self.assertEqual(on[0], (532 + 489, 151 + 250))
        self.assertEqual(len(on), len(off) + 1)
        self.assertEqual(on[1:], off)

    def test_redirect_home_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"ABF_PICKER_HOME": "0"}):
            env = self.hd.redirect_home({"USERPROFILE": "C:\\real"})
        self.assertEqual(env["USERPROFILE"], "C:\\real")

    def make_home(self):
        home = Path(tempfile.mkdtemp(prefix="abfhome"))
        self.addCleanup(shutil.rmtree, str(home), True)
        (home / "Documents" / "Abiotic Factor Mods").mkdir(parents=True)
        (home / "AppData" / "Local").mkdir(parents=True)
        (home / "AppData" / "Roaming").mkdir(parents=True)
        return home

    def test_remove_home_unlinks_only_the_two_links_and_never_recurses(self):
        home = self.make_home()
        links = {str(home / "AppData" / "Local"), str(home / "AppData" / "Roaming")}
        removed_with = []
        real_rmdir = os.rmdir

        def rmdir(p, *a, **k):
            removed_with.append(str(p))
            return real_rmdir(p, *a, **k)

        with mock.patch.object(self.hd, "_is_link", side_effect=lambda p: str(p) in links), \
                mock.patch.object(shutil, "rmtree") as rmtree, \
                mock.patch.object(self.hd.os, "rmdir", side_effect=rmdir):
            removed, still = self.hd.remove_home(home)
        self.assertEqual(sorted(removed), sorted(links))
        self.assertEqual(still, [])
        self.assertEqual(sorted(removed_with), sorted(links))       # os.rmdir: the link itself, never its contents
        rmtree.assert_not_called()
        self.assertFalse((home / "AppData" / "Local").exists())
        self.assertTrue((home / "Documents" / "Abiotic Factor Mods").is_dir())

    def test_remove_home_leaves_real_folders_alone_and_reports_links_it_cannot_remove(self):
        home = self.make_home()
        (home / "AppData" / "Local" / "keep.txt").write_text("x", encoding="utf-8")
        self.assertFalse(self.hd._is_link(home / "AppData" / "Local"))          # a plain folder is not a link
        self.assertEqual(self.hd.remove_home(home), ([], []))
        self.assertTrue((home / "AppData" / "Local" / "keep.txt").is_file())
        with mock.patch.object(self.hd, "_is_link", return_value=True), \
                mock.patch.object(self.hd.os, "rmdir", side_effect=OSError("busy")), \
                mock.patch.object(self.hd.os, "unlink", side_effect=OSError("busy")):
            removed, still = self.hd.remove_home(home)
        self.assertEqual(removed, [])
        self.assertEqual(len(still), 2)
        self.assertEqual(self.hd.remove_home(home / "nothing"), ([], []))

    def test_run_editor_retries_once_when_the_picker_mounted_another_mod(self):
        stage = SimpleNamespace(name="TestModXyz34")
        bad = {"ok": False, "error": "picker mounted OtherModAbc12 instead of TestModXyz34"}
        good = {"ok": True, "run": "r"}
        with mock.patch.object(self.hd, "_run_editor_once", side_effect=[bad, good]) as once:
            out = self.hd.run_editor(stage, {"op": "audit"})
        self.assertEqual(once.call_count, 2)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mounted"], "TestModXyz34")

    def test_run_editor_gives_up_after_one_retry_and_keeps_the_reason(self):
        stage = SimpleNamespace(name="TestModXyz34")
        bad = {"ok": False, "error": "picker mounted OtherModAbc12 instead of TestModXyz34"}
        with mock.patch.object(self.hd, "_run_editor_once", side_effect=[bad, dict(bad)]) as once:
            out = self.hd.run_editor(stage, {"op": "audit"})
        self.assertEqual(once.call_count, 2)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"].startswith("picker mounted "))
        self.assertNotIn("mounted", out)

    def test_run_editor_does_not_retry_other_failures(self):
        stage = SimpleNamespace(name="TestModXyz34")
        with mock.patch.object(self.hd, "_run_editor_once", return_value={"ok": False, "error": "timeout"}) as once:
            out = self.hd.run_editor(stage, {"op": "audit"})
        self.assertEqual(once.call_count, 1)
        self.assertFalse(out["ok"])

    def test_mod_audit_result_names_the_stage_and_the_mounted_mod(self):
        import headless_tools
        run = Path(tempfile.mkdtemp(prefix="abfaudit"))
        try:
            log = run / "editor.log"
            log.write_text("LogBlueprint: Display: Asset audit complete: 0 Error(s), 0 Warning(s)\n", encoding="utf-8")
            res = {"ok": True, "run": str(run), "log": str(log), "seconds": 1.0, "mounted": "Mod"}
            tools = {t[0]: t for t in headless_tools.build(lambda *a, **k: None, (RuntimeError,), lambda text, prefix=None: {"problems": []},
                                                            ROOT / "editor", [], "", list_processes=lambda: [])}
            self.assertEqual(tools["mod_audit"][3]["stage"], {"type": "boolean"})
            with mock.patch.object(headless.staging, "load", return_value=object()), \
                    mock.patch.object(headless.staging, "strip_tooling"), \
                    mock.patch.object(self.hd, "run_editor", return_value=res) as re_:
                text, _ = tools["mod_audit"][1]({"mod": "Mod", "stage": True})
            d = json.loads(text)
            self.assertEqual((d["audited_copy"], d["mounted"]), ("stage", "Mod"))
            self.assertEqual(re_.call_args[0][1], {"op": "audit"})
        finally:
            shutil.rmtree(str(run), ignore_errors=True)

    def test_backend_info_reports_stage_audit(self):
        self.assertIs(BackendInfoCapabilityTests().capabilities(False)["stageAudit"], True)


class ClassManagerRowSpecTests(unittest.TestCase):
    """The class_manager_row asset-spec key: validation on the host, no editor."""
    ROW = "/TestMod/DataTables/DT_ClassManager/EntityDirector_Test"
    CLS = "/TestMod/Feature/BPC_Test.BPC_Test_C"

    def validate(self, row, **extra):
        from bpkit import specs
        spec = {"asset": "/TestMod/Feature/BPC_Test", "class_manager_row": row}
        spec.update(extra)
        return specs.validate({"assets": [spec]}, "TestMod")

    def good(self):
        return {"asset_path": self.ROW, "class_path": self.CLS}

    def test_good_entry_is_accepted(self):
        self.assertEqual(self.validate(self.good()), [])

    def test_feature_is_listed_in_sorted_order(self):
        from bpkit import specs
        self.assertIn("class_manager_row", specs.FEATURES)
        self.assertEqual(list(specs.FEATURES), sorted(specs.FEATURES))

    def test_game_paths_are_refused(self):
        self.assertTrue(any("/Game" in p for p in self.validate(dict(self.good(), asset_path="/Game/DataTables/Row"))))
        self.assertTrue(any("/Game" in p for p in self.validate(dict(self.good(), class_path="/Game/Feature/BP_X.BP_X_C"))))

    def test_other_mod_roots_are_refused(self):
        self.assertTrue(any("must start with /TestMod/" in p for p in self.validate(dict(self.good(), asset_path="/OtherMod/Tables/Row"))))
        self.assertTrue(any("must start with /TestMod/" in p for p in self.validate(dict(self.good(), class_path="/OtherMod/Feature/BP_X.BP_X_C"))))

    def test_bad_class_path_shapes(self):
        for bad in ("/TestMod/Feature/BPC_Test", "/TestMod/Feature/BPC_Test.BPC_Test", "/TestMod/Feature/BPC Test.BPC_Test_C",
                    "TestMod/Feature/BPC_Test.BPC_Test_C"):
            self.assertTrue(self.validate(dict(self.good(), class_path=bad)), bad)

    def test_bad_asset_path_shapes(self):
        for bad in ("/TestMod", "/TestMod/Row.Row", "/TestMod/Tables/Row/", "/TestMod/Tables/Ro-w", "TestMod/Tables/Row"):
            self.assertTrue(self.validate(dict(self.good(), asset_path=bad)), bad)

    def test_missing_keys_and_wrong_types(self):
        self.assertTrue(any("asset_path is required" in p for p in self.validate({"class_path": self.CLS})))
        self.assertTrue(any("class_path is required" in p for p in self.validate({"asset_path": self.ROW})))
        self.assertTrue(any("must be an object" in p for p in self.validate("nope")))
        self.assertTrue(any("unknown key" in p for p in self.validate(dict(self.good(), extra=1))))
        self.assertTrue(self.validate(dict(self.good(), asset_path=5)))

    def test_not_valid_on_a_struct_spec(self):
        from bpkit import specs
        spec = {"asset": "/TestMod/Feature/S_X", "create": {"class": "UserDefinedStruct"}, "class_manager_row": self.good(),
                "struct_fields": [{"name": "A_0_" + "0" * 32, "friendly": "A", "guid": "0" * 32, "type": "int"}]}
        self.assertTrue(any("class_manager_row" in p for p in specs.validate({"assets": [spec]}, "TestMod")))

    def test_backend_info_reports_the_capability(self):
        caps = BackendInfoCapabilityTests().capabilities(False)
        self.assertIs(caps["classManagerRows"], True)

    def test_gameplay_tag_validation(self):
        from bpkit import specs
        import tagsini
        self.assertEqual(specs.TAG_RX, tagsini.TAG_RX)
        self.assertIn("class_manager_row_tag", specs.FEATURES)
        for ok in ("ClassManager.EntityDirector.Test", "A", "A-b_c.D1"):
            self.assertEqual(self.validate(dict(self.good(), gameplay_tag=ok)), [], ok)
        for bad in ("", "a..b", "a b", "a/b", ".a", "a.", "x" * 201, 5, None, "a\"b"):
            self.assertTrue(any("gameplay_tag" in p for p in self.validate(dict(self.good(), gameplay_tag=bad))), repr(bad))

    def test_server_version_is_2_5_0(self):
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('SERVER_VERSION = "2.5.0"', text)


class ClassManagerRowEditorSideTests(unittest.TestCase):
    """editor/bpkit/rows.py with a stubbed `unreal` module."""

    ROW = "/TestMod/DataTables/DT_ClassManager/EntityDirector_Test"
    CLS = "/TestMod/Feature/BPC_Test.BPC_Test_C"

    class Cls:
        def __init__(self, path):
            self.path = path

        def get_path_name(self):
            return self.path

    class RowData:
        def __init__(self, cls=None):
            self.props = {"object_class": cls, "gameplay_tag": "", "release_group": 0}

        def get_editor_property(self, key):
            return self.props[key]

        def set_editor_property(self, key, value):
            self.props[key] = value

    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)
        import bpkit
        import importlib
        for short in ("core", "tags", "rows"):
            name = "bpkit." + short
            sys.modules.pop(name, None)
            if hasattr(bpkit, short):
                delattr(bpkit, short)        # `from . import core` would otherwise return the module bound to an older stub
            self.addCleanup(sys.modules.pop, name, None)
            self.addCleanup(lambda s=short: hasattr(bpkit, s) and delattr(bpkit, s))
        self.rows = importlib.import_module("bpkit.rows")
        self.u = sys.modules["unreal"]
        outer = self

        class FakeRowAsset:
            def __init__(self, row):
                self.props = {"row": row, "row_name": None}
                self.saved = False

            def get_editor_property(self, key):
                return self.props[key]

            def set_editor_property(self, key, value):
                self.props[key] = value

            def get_class(self):
                return mock.MagicMock()

        self.FakeRowAsset = FakeRowAsset
        self.u.ClassManagerModRowAsset = FakeRowAsset
        self.u.load_class.return_value = self.Cls(self.CLS)
        self.u.EditorAssetLibrary.save_loaded_asset.return_value = True
        self.outer = outer

    def test_missing_row_is_created_with_name_and_class(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = False
        made = self.FakeRowAsset(self.RowData())
        u.AssetToolsHelpers.get_asset_tools.return_value.create_asset.return_value = made
        res = self.rows.ensure_class_manager_row(self.ROW, self.CLS)
        self.assertEqual(res, {"asset": self.ROW, "created": True, "changed": True, "tag": None})
        self.assertEqual(made.props["row_name"], "EntityDirector_Test")
        self.assertEqual(made.props["row"].props["object_class"].get_path_name(), self.CLS)
        self.assertEqual(made.props["row"].props["release_group"], 0)
        args = u.AssetToolsHelpers.get_asset_tools.return_value.create_asset.call_args[0]
        self.assertEqual(args[:2], ("EntityDirector_Test", "/TestMod/DataTables/DT_ClassManager"))
        u.EditorAssetLibrary.save_loaded_asset.assert_called_once()

    def test_row_that_already_references_the_class_is_left_alone(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        existing = self.FakeRowAsset(self.RowData(self.Cls(self.CLS)))
        u.load_asset.return_value = existing
        res = self.rows.ensure_class_manager_row(self.ROW, self.CLS)
        self.assertEqual(res, {"asset": self.ROW, "created": False, "changed": False, "tag": None})
        u.EditorAssetLibrary.save_loaded_asset.assert_not_called()

    def test_row_with_another_class_changes_only_the_class(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        data = self.RowData(self.Cls("/TestMod/Other/BP_Old.BP_Old_C"))
        data.props["release_group"] = 3
        existing = self.FakeRowAsset(data)
        u.load_asset.return_value = existing
        res = self.rows.ensure_class_manager_row(self.ROW, self.CLS)
        self.assertEqual(res, {"asset": self.ROW, "created": False, "changed": True, "tag": None})
        self.assertEqual(existing.props["row"].props["object_class"].get_path_name(), self.CLS)
        self.assertEqual(existing.props["row"].props["release_group"], 3)
        u.EditorAssetLibrary.save_loaded_asset.assert_called_once()

    TAG = "ClassManager.EntityDirector.Test"

    class FakeTag:
        """A GameplayTag struct: import_text sets the name, export_text reads it (the first strategy of tags.make_tag)."""
        refuse = False

        def __init__(self, name=""):
            self.name = name

        def import_text(self, text):
            if not self.refuse:
                self.name = text.split('"')[1]

        def export_text(self):
            return '(TagName="%s")' % (self.name or "None")

    def tag_row(self, name=""):
        data = self.RowData(self.Cls(self.CLS))
        data.props["gameplay_tag"] = self.FakeTag(name)
        return self.FakeRowAsset(data)

    def test_tag_is_set_when_different_and_saved(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        u.GameplayTag = self.FakeTag
        existing = self.tag_row("ClassManager.EntityDirector.Old")
        u.load_asset.return_value = existing
        res = self.rows.ensure_class_manager_row(self.ROW, self.CLS, self.TAG)
        self.assertEqual(res, {"asset": self.ROW, "created": False, "changed": True, "tag": self.TAG})
        self.assertEqual(existing.props["row"].props["gameplay_tag"].name, self.TAG)
        u.EditorAssetLibrary.save_loaded_asset.assert_called_once()

    def test_unchanged_tag_is_left_alone(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        u.GameplayTag = self.FakeTag
        u.load_asset.return_value = self.tag_row(self.TAG)
        res = self.rows.ensure_class_manager_row(self.ROW, self.CLS, self.TAG)
        self.assertEqual(res, {"asset": self.ROW, "created": False, "changed": False, "tag": self.TAG})
        u.EditorAssetLibrary.save_loaded_asset.assert_not_called()

    def test_tag_that_cannot_be_verified_is_an_error_and_nothing_is_saved(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        u.GameplayTag = self.FakeTag
        self.FakeTag.refuse = True
        self.addCleanup(setattr, self.FakeTag, "refuse", False)
        u.load_asset.return_value = self.tag_row("")
        with self.assertRaises(RuntimeError) as cm:
            self.rows.ensure_class_manager_row(self.ROW, self.CLS, self.TAG)
        self.assertIn("cannot set gameplay tag", str(cm.exception))
        u.EditorAssetLibrary.save_loaded_asset.assert_not_called()

    def test_game_paths_and_foreign_assets_are_refused(self):
        u = self.u
        with self.assertRaises(RuntimeError):
            self.rows.ensure_class_manager_row("/Game/Tables/Row", self.CLS)
        u.EditorAssetLibrary.does_asset_exist.return_value = True
        u.load_asset.return_value = mock.MagicMock()
        with self.assertRaises(RuntimeError):
            self.rows.ensure_class_manager_row(self.ROW, self.CLS)

    def test_unsaved_row_is_an_error(self):
        u = self.u
        u.EditorAssetLibrary.does_asset_exist.return_value = False
        u.AssetToolsHelpers.get_asset_tools.return_value.create_asset.return_value = self.FakeRowAsset(self.RowData())
        u.EditorAssetLibrary.save_loaded_asset.return_value = False
        with self.assertRaises(RuntimeError):
            self.rows.ensure_class_manager_row(self.ROW, self.CLS)

    def test_editor_side_files_compile(self):
        tmp = Path(tempfile.mkdtemp(prefix="abfrows"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        for rel in ("editor/bpkit/rows.py", "editor/bpkit/jobs.py"):
            py_compile.compile(str(ROOT / rel), cfile=str(tmp / "x.pyc"), doraise=True)


from tests.test_asset_ops import AssetOpsEditorSideTests, AssetOpsHostTests, AssetOpsSpecTests  # noqa: E402,F401  (bp_asset_ops tests)


if __name__ == "__main__":
    unittest.main()
