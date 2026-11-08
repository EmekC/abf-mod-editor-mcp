"""catalog op (hidden editor, commandlet): names-only catalog of /Game Blueprint classes and /Script classes, structs, enums
and function libraries.

Reads only: the asset registry (tags, no asset loads) and ObjectIterator over already loaded /Script objects. Writes
<run>/catalog.json (format: catalogparse.assemble) and returns {"file", "counts", "seconds"}. A failing item goes to "errors"
and never aborts the run.
"""
import collections
import json
import os
import time

import unreal

from . import catalogparse as cp

SCRIPT = "/Script/"
FUNCTION_LIBRARY = ("/Script/Engine", "BlueprintFunctionLibrary")
GENERATED_CLASS = ("/Script/Engine", "BlueprintGeneratedClass")


def _asset_path(x):
    return "%s.%s" % (x.package_name, x.asset_name)


def _top(path):
    package, name = path.rsplit(".", 1)
    return unreal.TopLevelAssetPath(package, name)


def _game_classes(ar, errors):
    out = []
    assets = ar.get_assets(unreal.ARFilter(package_paths=["/Game"], recursive_paths=True,
                                           class_paths=[unreal.TopLevelAssetPath(*GENERATED_CLASS)], recursive_classes=True))
    for a in assets:
        try:
            out.append(cp.game_entry(a.package_name, a.asset_name, a.asset_class_path.asset_name,
                                     a.get_tag_value("ParentClass"), a.get_tag_value("ClassFlags")))
        except Exception as e:
            errors.append("game class %s.%s: %s" % (a.package_name, a.asset_name, str(e)[:200]))
    return out


def _script_objects(kind, type_name, errors):
    """{path name: object} of loaded /Script objects whose python type is exactly `type_name` (no wrappers of subclasses, no Default__)."""
    found = {}
    try:
        for o in unreal.ObjectIterator(kind):
            p = o.get_path_name()
            if p.startswith(SCRIPT) and ".Default__" not in p and type(o).__name__ == type_name:
                found[p] = o
    except Exception as e:
        errors.append("%s iteration: %s" % (type_name, str(e)[:200]))
    return found


def _function_names(errors):
    """{class path name: [function names]} from one pass over all fields (UFunctions show up with python type 'Struct')."""
    funcs = collections.defaultdict(list)
    try:
        for f in unreal.ObjectIterator(unreal.Field):
            try:
                if type(f).__name__ != "Struct":
                    continue
                outer = f.get_outer()
                if outer is None:
                    continue
                op = outer.get_path_name()
                if op.startswith(SCRIPT):
                    funcs[op].append(f.get_name())
            except Exception:
                continue
    except Exception as e:
        errors.append("function iteration: %s" % str(e)[:200])
    return funcs


def _mro_parent(cls):
    """(nearest python-wrapped ancestor path or None, whether BlueprintFunctionLibrary is in the CDO's MRO) for a class the registry hierarchy does not know."""
    mro = type(unreal.get_default_object(cls)).__mro__
    parent = None
    for k in mro[1:]:
        sc = getattr(k, "static_class", None)
        if sc is None:
            continue
        kc = sc()
        if kc is not None:
            parent = kc.get_path_name()
            break
    return parent, any(k.__name__ == "BlueprintFunctionLibrary" for k in mro)


def _script_class_entries(ar, classes, funcs, errors):
    libraries = set()
    try:
        libraries = {_asset_path(x) for x in ar.get_derived_class_names([unreal.TopLevelAssetPath(*FUNCTION_LIBRARY)], set())}
    except Exception as e:
        errors.append("function library query: %s" % str(e)[:200])
    out = []
    for p in sorted(classes):
        parent = None
        is_lib = p in libraries
        try:
            anc = ar.get_ancestor_class_names(_top(p))
            parent = cp.nearest_parent(p, [_asset_path(x) for x in anc] if anc else [])
        except Exception as e:
            errors.append("ancestors of %s: %s" % (p, str(e)[:200]))
        if parent is None and p != "/Script/CoreUObject.Object":
            try:
                parent, mro_lib = _mro_parent(classes[p])
                is_lib = is_lib or mro_lib
            except Exception:
                pass
        out.append(cp.script_entry(p, parent, is_lib, funcs.get(p, [])))
    return out


def run(job):
    t0 = time.time()
    errors = []
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    game = _game_classes(ar, errors)
    classes = _script_objects(unreal.Class, "Class", errors)
    structs = _script_objects(unreal.ScriptStruct, "ScriptStruct", errors)
    enums = _script_objects(unreal.Enum, "Enum", errors)
    entries = _script_class_entries(ar, classes, _function_names(errors), errors)
    doc = cp.assemble(unreal.SystemLibrary.get_engine_version(), game, entries, list(structs), list(enums), errors)
    with open(os.path.join(job["run"], "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
    return {"file": "catalog.json", "counts": doc["counts"], "seconds": round(time.time() - t0, 3)}
