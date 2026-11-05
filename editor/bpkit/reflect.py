"""reflect op (hidden editor, commandlet): functions, properties, struct fields, ancestors and cast pins of classes.

Reads only: classes are loaded with unreal.load_class (never load_asset, never an export of a /Game asset). Pin lists
come from throwaway nodes in a scratch Blueprint (graphs.make_templates), parsed by reflectparse. Writes
<run>/reflect.json and returns {"file", "counts"}. A failing member goes to "errors" and never aborts the run.
"""
import json
import os

import unreal

from . import core, graphs, reflectparse as rp, t3d

CHUNK = 300
CALL = "/Script/BlueprintGraph.K2Node_CallFunction"
GET = "/Script/BlueprintGraph.K2Node_VariableGet"
BREAK = "/Script/BlueprintGraph.K2Node_BreakStruct"
CAST = "/Script/BlueprintGraph.K2Node_DynamicCast"
DEFAULTS = "/Script/BlueprintGraph.K2Node_GetClassDefaults"


def _load_path(path):
    """/Game/Dir/BP_X.BP_X_C or /Game/Dir/BP_X (-> .BP_X_C); /Script/Mod.Class stays as it is."""
    if path.startswith("/Script/"):
        return path
    last = path.rsplit("/", 1)[-1]
    return path if "." in last else "%s.%s_C" % (path, last)


def _class_text(path, meta):
    if path.startswith("/Script/"):
        return t3d.class_ref(path)
    return t3d.class_ref(path, meta or "/Script/Engine.BlueprintGeneratedClass")


def _templates(items, errors, what):
    """items {key: (class, body)} -> {key: [pin dicts]}; a failing chunk is retried item by item."""
    out = {}
    keys = list(items)
    for i in range(0, len(keys), CHUNK):
        part = {k: items[k] for k in keys[i:i + CHUNK]}
        try:
            raw = graphs.make_templates(part)
            for k, lines in raw.items():
                out[k] = rp.parse_pins(lines)
        except Exception as e:
            errors.append("%s chunk of %d failed (%s); retrying one by one" % (what, len(part), str(e)[:200]))
            for k, v in part.items():
                try:
                    out[k] = rp.parse_pins(graphs.make_templates({k: v})[k])
                except Exception as e2:
                    errors.append("%s %s failed: %s" % (what, k, str(e2)[:200]))
    return out


def _function_index(wanted):
    """{class path name: [function names]} in one pass over all fields (UFunctions show up with python type 'Struct')."""
    found = {}
    for f in unreal.ObjectIterator(unreal.Field):
        try:
            if type(f).__name__ != "Struct":
                continue
            outer = f.get_outer()
            key = outer.get_path_name() if outer else ""
            if key in wanted:
                found.setdefault(key, []).append(f.get_name())
        except Exception:
            continue
    return found


def _ancestors(cls, errors=None):
    """Native class paths of the CDO's Python wrapper chain, nearest first. A wrapper whose static_class() is None
    (or raises) is skipped; the chain is not walkable for Blueprint-only parents in this build."""
    try:
        cdo = unreal.get_default_object(cls)
        out = []
        for c in type(cdo).__mro__:
            sc = getattr(c, "static_class", None)
            if sc is None:
                continue
            try:
                k = sc()
                path = k.get_path_name() if k is not None else None
            except Exception:
                path = None
            if path and path not in out:
                out.append(path)
        return out
    except Exception as e:
        if errors is not None:
            errors.append("ancestors of %s: %s" % (cls.get_path_name(), str(e)[:200]))
        return []


def run(job):
    errors = []
    classes_in = list(job.get("classes") or [])
    structs_in = list(job.get("structs") or [])
    casts_in = list(job.get("cast_targets") or [])
    loaded = {}   # requested path -> (class object, path name, meta)
    for p in classes_in + [c for c in casts_in if c not in classes_in]:
        try:
            cls = unreal.load_class(None, _load_path(p))
            if cls is None:
                errors.append("class %s could not be loaded" % p)
                continue
            loaded[p] = (cls, cls.get_path_name(), cls.get_class().get_path_name())
        except Exception as e:
            errors.append("class %s: %s" % (p, str(e)[:200]))

    funcs_by_outer = _function_index({v[1] for k, v in loaded.items() if k in classes_in})

    result_classes = {}
    call_items, key_info = {}, {}
    defaults_items = {}
    for p in classes_in:
        if p not in loaded:
            continue
        cls, pname, meta = loaded[p]
        names = sorted(n for n in funcs_by_outer.get(pname, []) if not rp.SKIP_FUNCTION(n))
        result_classes[p] = {"meta": meta, "ancestors": _ancestors(cls, errors), "functions": [], "properties": [], "castPin": None}
        ct = _class_text(pname, meta)
        for n in names:
            key = "f%d" % len(call_items)
            call_items[key] = (CALL, rp.call_body(ct, n))
            key_info[key] = (p, "functions", n)
        defaults_items["d%d" % len(defaults_items)] = (DEFAULTS, [rp.class_pin_line(pname, meta)])
        key_info["d%d" % (len(defaults_items) - 1)] = (p, "defaults", None)

    for key, pins in _templates(call_items, errors, "function").items():
        p, _slot, n = key_info[key]
        result_classes[p]["functions"].append({"name": n, "pins": pins})

    # properties: the output pins of a GetClassDefaults node name the readable properties; one VariableGet each gives the pins
    get_items = {}
    for key, pins in _templates(defaults_items, errors, "class defaults").items():
        p = key_info[key][0]
        _cls, pname, meta = loaded[p]
        ct = _class_text(pname, meta)
        for pin in pins:
            if pin["dir"] == "out" and pin["category"] != "exec" and not pin["hidden"]:
                gk = "p%d" % len(get_items)
                get_items[gk] = (GET, rp.get_body(ct, pin["name"]))
                key_info[gk] = (p, "properties", pin["name"])
    for key, pins in _templates(get_items, errors, "property").items():
        p, _slot, n = key_info[key]
        result_classes[p]["properties"].append({"name": n, "pins": pins})

    # cast pins: one K2Node_DynamicCast per class (listed classes and cast targets)
    cast_items = {}
    for p in classes_in + [c for c in casts_in if c not in classes_in]:
        if p not in loaded:
            continue
        _cls, pname, meta = loaded[p]
        cast_items["c%d" % len(cast_items)] = (CAST, rp.cast_body(_class_text(pname, meta)))
        key_info["c%d" % (len(cast_items) - 1)] = (p, "cast", None)
    for key, pins in _templates(cast_items, errors, "cast").items():
        p = key_info[key][0]
        if p not in result_classes:
            _cls, pname, meta = loaded[p]
            result_classes[p] = {"meta": meta, "ancestors": _ancestors(loaded[p][0], errors), "functions": [], "properties": [],
                                 "castPin": None, "castOnly": True}
        result_classes[p]["castPin"] = rp.cast_pin(pins)

    # struct fields: the output pins of a BreakStruct node
    struct_items = {}
    for s in structs_in:
        struct_items["s%d" % len(struct_items)] = (BREAK, rp.break_body(s))
        key_info["s%d" % (len(struct_items) - 1)] = (s, "struct", None)
    result_structs = {}
    for key, pins in _templates(struct_items, errors, "struct").items():
        result_structs[key_info[key][0]] = {"fields": [x for x in pins if x["dir"] == "out" and x["category"] != "exec"]}

    out = {"engineVersion": unreal.SystemLibrary.get_engine_version(), "classes": result_classes,
           "structs": result_structs, "errors": errors}
    with open(os.path.join(job["run"], "reflect.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    counts = {"classes": len(result_classes),
              "functions": sum(len(c["functions"]) for c in result_classes.values()),
              "properties": sum(len(c["properties"]) for c in result_classes.values()),
              "structs": len(result_structs), "errors": len(errors)}
    return {"file": "reflect.json", "counts": counts}
