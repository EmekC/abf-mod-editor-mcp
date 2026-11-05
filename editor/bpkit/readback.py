"""Live read of what the Blueprint text export leaves out: SCS components and variable defaults.

The text export has neither SCS nodes nor variable defaults, so they are read here from the live objects. Every
function that takes values from the engine is read-only.
"""
import re

import unreal

from .core import LIB, must, pth


def variable_lines(text):
    """[(name, pin_category)] of the Blueprint's own NewVariables lines."""
    return re.findall(r'^\s*NewVariables\(\d+\)=\(VarName="([^"]+)".*?PinCategory="([^"]*)"', text, re.M)


def _quote(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def default_text(v, nested=False):
    """A value in bp_apply's spelling: "10", "true", "2.5", "hello", struct export_text(), object path, (a,b)."""
    if v is None:
        return "None" if nested else None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _quote(v) if nested else v
    if isinstance(v, (unreal.Name, unreal.Text)):
        return _quote(str(v)) if nested else str(v)
    if isinstance(v, unreal.EnumBase):
        return getattr(v, "name", None) or str(v)
    if isinstance(v, unreal.Object):
        return pth(v)
    if isinstance(v, unreal.StructBase):
        return v.export_text()
    if isinstance(v, (unreal.Array, unreal.Set)):
        return "(" + ",".join(default_text(x, True) for x in v) + ")"
    if isinstance(v, unreal.Map):
        return "(" + ",".join("(%s, %s)" % (default_text(k, True), default_text(x, True)) for k, x in v.items()) + ")"
    return str(v)


def _class_and_default_object(bp):
    gc = must(LIB.generated_class(bp), "generated class")
    cdo = must(unreal.get_default_object(gc), "class default object")
    return gc, cdo


def _holds_none(v):
    """True when a container (array, set, map) has a None element, key or value."""
    if isinstance(v, unreal.Map):
        return any(k is None or x is None for k, x in v.items())
    if isinstance(v, (unreal.Array, unreal.Set)):
        return any(x is None for x in v)
    return False


def read_defaults(bp, lines):
    """{"status": "ok", "items": {name: {"status", "default"? , "error"?}}} read from the class default object."""
    _gc, cdo = _class_and_default_object(bp)
    items = {}
    for name, cat in lines:
        if cat == "mcdelegate":
            items[name] = {"status": "not_applicable"}
            continue
        try:
            v = cdo.get_editor_property(name)
            if v is None and cat in ("softobject", "softclass"):
                items[name] = {"status": "unavailable", "error": "soft reference: not loaded or empty, cannot tell which"}
            elif cat in ("softobject", "softclass") and _holds_none(v):
                items[name] = {"status": "unavailable", "error": "soft reference in a container: an element is not loaded or empty, cannot tell which"}
            else:
                items[name] = {"status": "ok", "default": default_text(v)}
        except Exception as e:
            items[name] = {"status": "unavailable", "error": "%s: %s" % (type(e).__name__, e)}
    return {"status": "ok", "items": items}


def immediate_parents(ancestors):
    """ancestors[i] = row indexes row i is attached to (direct or transitive). Returns the nearest one per row."""
    return [max(a, key=lambda j: len(ancestors[j]), default=None) for a in ancestors]


def read_components(bp):
    """{"status": "ok"|"partial", "items": [...], "note"?}. A class that cannot have components gives [] and a note."""
    L = unreal.SubobjectDataBlueprintFunctionLibrary
    gc, cdo = _class_and_default_object(bp)
    if not isinstance(cdo, unreal.Actor):
        return {"status": "ok", "items": [], "note": "%s is not an Actor class, so it has no components" % gc.get_name()}
    ss = must(unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem), "subobject subsystem")
    hs = list(ss.k2_gather_subobject_data_for_blueprint(bp))
    if not hs:
        raise RuntimeError("the engine returned no subobject data")
    datas = [ss.k2_find_subobject_data_from_handle(h) for h in hs]
    # one row per component. The engine's gather can list the same component twice (seen live), so rows with the same
    # name AND the same object are merged, keeping every handle. A "None" name (native component without a property)
    # or an unreadable object is never merged.
    rows, names, objs, keys = [], [], [], {}
    for h, d in zip(hs, datas):
        if d is None or not L.is_component(d):
            continue
        n = str(L.get_variable_name(d))
        obj = L.get_object_for_blueprint(d, bp)
        if obj is None:
            obj = L.get_object(d)
        key = (n, pth(obj)) if obj is not None and n != "None" else None
        if key is not None and key in keys:
            rows[keys[key]].append((h, d))
            continue
        if key is not None:
            keys[key] = len(rows)
        rows.append([(h, d)])
        names.append(n)
        objs.append(obj)
    ancestors = [[j for j, rj in enumerate(rows)
                  if j != i and any(L.is_attached_to(d, hj) for _, d in ri for hj, _ in rj)]
                 for i, ri in enumerate(rows)]
    parents = immediate_parents(ancestors)
    items = []
    for i, ri in enumerate(rows):
        d, obj = ri[0][1], objs[i]
        items.append({"name": names[i],
                      "class": pth(obj.get_class()) if obj is not None else None,
                      "parent": names[parents[i]] if parents[i] is not None else None,
                      "inherited": bool(L.is_inherited_component(d)),
                      "native": bool(L.is_native_component(d)),
                      "default_scene_root": bool(L.is_default_scene_root(d))})
    partial = any(d is None for d in datas) or any(it["class"] is None for it in items)
    return {"status": "partial" if partial else "ok", "items": items}


def _unavailable(e):
    return {"status": "unavailable", "error": "%s: %s" % (type(e).__name__, e)}


def read_all(bp, export_text):
    """{"defaults": ..., "components": ...}. Never raises: a failed part becomes {"status": "unavailable", "error"}."""
    out = {}
    try:
        out["defaults"] = read_defaults(bp, variable_lines(export_text))
    except Exception as e:
        out["defaults"] = _unavailable(e)
    try:
        out["components"] = read_components(bp)
    except Exception as e:
        out["components"] = _unavailable(e)
    return out
