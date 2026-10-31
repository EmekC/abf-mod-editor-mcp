"""Hidden game test: a generator driven by the tick in jobs.py. Python cannot create widgets or spawn actors in -game,
so a harness Blueprint (SpawnAny / MakeAny, built by the host in the mod's _McpTest folder) does it.

Each step records {i, op, ok, value} to <run>/progress.jsonl as soon as it finishes, so a killed run keeps its progress.
"""
import json
import os
import re
import time

import unreal

from .core import must


def _snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _jsonable(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def _class_path(p):
    last = p.rsplit("/", 1)[-1]
    return p if "." in last else "%s.%s_C" % (p, last)


def game_test(job):
    run, mod = job["run"], job["mod"]
    map_path = job.get("map") or "/Game/Maps/Empty"
    needle = map_path[len("/Game/"):] if map_path.startswith("/Game/") else map_path.rsplit("/", 1)[-1]
    t0 = time.time()
    level = None
    while level is None:
        for a in unreal.ObjectIterator(unreal.Actor):
            s = str(a)
            if needle in s and "PersistentLevel" in s:
                level = a.get_outer()
                break
        if level is None:
            if time.time() - t0 > 120:
                raise RuntimeError("map %s did not load within 120 s" % map_path)
            yield 0.5
    ctx = {}
    state = {"harness": None}
    progress = open(os.path.join(run, "progress.jsonl"), "a", encoding="utf-8")

    def harness():
        if state["harness"] is None:
            cls = must(unreal.load_class(None, "/%s/_McpTest/BP_McpHarness.BP_McpHarness_C" % mod), "harness class")
            state["harness"] = unreal.new_object(cls, outer=level)
        return state["harness"]

    def resolve(v):
        if isinstance(v, str) and v.startswith("$"):
            if v[1:] not in ctx:
                raise KeyError("no value named %s (set one with 'as')" % v)
            return ctx[v[1:]]
        if isinstance(v, dict):
            if "class" in v:
                return must(unreal.load_class(None, _class_path(v["class"])), "class " + v["class"])
            if "text" in v:
                return unreal.Text(v["text"])
        if isinstance(v, list):
            return [resolve(x) for x in v]
        return v

    def get_class(step):
        return must(unreal.load_class(None, _class_path(step["class"])), "class " + step["class"])

    results = []
    for i, step in enumerate(job.get("steps") or []):
        op = step.get("op")
        rec = {"i": i, "op": op, "ok": True, "value": None}
        try:
            value = None
            if op == "new":
                value = unreal.new_object(get_class(step), outer=level)
            elif op == "spawn":
                value = harness().call_method("SpawnAny", (get_class(step),))
                must(value, "spawned actor")
            elif op == "widget":
                value = harness().call_method("MakeAny", (get_class(step),))
                must(value, "created widget")
                if step.get("viewport"):
                    value.add_to_viewport()
            elif op == "call":
                target = resolve(step["target"])
                args = [resolve(a) for a in step.get("args") or []]
                fn = step["function"]
                snake = _snake(fn)
                if hasattr(target, snake):
                    value = getattr(target, snake)(*args)
                else:
                    value = target.call_method(fn, tuple(args)) if args else target.call_method(fn)
            elif op == "get":
                value = resolve(step["target"]).get_editor_property(step["property"])
            elif op == "set":
                resolve(step["target"]).set_editor_property(step["property"], resolve(step.get("value")))
            elif op == "find":
                base = getattr(unreal, step.get("base", "Actor"))
                want = step.get("class_contains", "")
                deadline = time.time() + float(step.get("timeout", 120))
                while value is None:
                    for o in unreal.ObjectIterator(base):
                        s = str(o)
                        if "Default__" in s or want not in s:
                            continue
                        value = o
                        break
                    if value is None:
                        if time.time() > deadline:
                            raise RuntimeError("no %s containing %r within %ss" % (step.get("base", "Actor"), want, step.get("timeout", 120)))
                        yield 0.5
            elif op == "wait":
                yield float(step.get("seconds", 1.0))
            elif op == "assert":
                v = resolve(step["value"])
                value = v
                if "equals" in step:
                    ok = v == step["equals"]
                elif "approx" in step:
                    ok = abs(float(v) - float(step["approx"])) <= float(step.get("tolerance", 0.01))
                elif "contains" in step:
                    ok = step["contains"] in (v if isinstance(v, (str, list, tuple)) else str(v))
                elif "truthy" in step:
                    ok = bool(v) == bool(step["truthy"])
                elif "not_none" in step:
                    ok = (v is not None) == bool(step["not_none"])
                else:
                    raise ValueError("assert needs equals, approx, contains, truthy or not_none")
                rec["ok"] = bool(ok)
            elif op == "python":
                scope = {"unreal": unreal, "ctx": ctx, "level": level}
                exec(compile(step["code"], "<game_test python>", "exec"), scope)
                value = scope.get("result")
            else:
                raise ValueError("unknown step op %r" % op)
            if step.get("as"):
                ctx[step["as"]] = value
            rec["value"] = _jsonable(value) if not isinstance(value, unreal.Object) else str(value)
        except Exception as e:                                   # the step failed; later steps still run
            rec["ok"] = False
            rec["error"] = "%s: %s" % (type(e).__name__, e)
        progress.write(json.dumps(rec, default=str) + "\n")
        progress.flush()
        results.append(rec)
    progress.close()
    return {"steps": results, "passed": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}
