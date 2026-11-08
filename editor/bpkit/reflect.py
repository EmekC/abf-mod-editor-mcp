"""reflect op (hidden editor, commandlet): functions, properties, struct fields, ancestors and cast pins of classes.

Reads only: classes are loaded with unreal.load_class (never load_asset, never an export of a /Game asset). Pin lists
come from throwaway nodes in a scratch Blueprint (graphs.make_templates), parsed by reflectparse. Members that
GetClassDefaults cannot list (widgets, components, widget/component variables) are found by _member_pass before the first
template run: candidate names from the widget tree, SCS templates and the class's cooked package (cookedpkg), native classes from
their Python docstring; each is checked on the class default object. Only widget (Visual) and ActorComponent typed members are
kept (timelines are kind "component"). A native member's name comes from the docstring (snake_case -> PascalCase) and may differ in
case from the real property name (acronyms); FNames compare case-insensitively, so the VariableGet lowering still resolves it. Writes
<run>/reflect.json and returns {"file", "counts"}. A failing member goes to "errors" and never aborts the run.
"""
import json
import os

import unreal

from . import cookedpkg, core, graphs, reflectparse as rp, t3d

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


def _function_index(wanted, objs=None):
    """{class path name: [function names]} in one pass over all fields (UFunctions show up with python type 'Struct').
    When `objs` is a dict it also receives {(class path name, function name): UFunction}."""
    found = {}
    for f in unreal.ObjectIterator(unreal.Field):
        try:
            if type(f).__name__ != "Struct":
                continue
            outer = f.get_outer()
            key = outer.get_path_name() if outer else ""
            if key in wanted:
                found.setdefault(key, []).append(f.get_name())
                if objs is not None:
                    objs[(key, f.get_name())] = f
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


def _supers(cls, ancestors):
    """Every parent class of `cls` (Blueprint and native), nearest first, ending at /Script/CoreUObject.Object, or None.
    Source: the asset registry's class inheritance; every link is checked with class_is_child_of. A /Script class whose
    registry chain does not verify falls back to the native `ancestors` list (checked the same way)."""
    try:
        path = cls.get_path_name()
        pkg, _, name = path.rpartition(".")
        reg = unreal.AssetRegistryHelpers.get_asset_registry()
        found = reg.get_ancestor_class_names(unreal.TopLevelAssetPath(pkg, name))
        names = ["%s.%s" % (str(n.package_name), str(n.asset_name)) for n in (found or [])]
        cache = {path: cls}

        def load(p):
            if p not in cache:
                cache[p] = unreal.load_class(None, p)
            return cache[p]

        def is_child(a, b):
            ca, cb = load(a), load(b)
            if ca is None or cb is None:
                return False
            return bool(unreal.MathLibrary.class_is_child_of(ca, cb))

        result = rp.verified_supers(path, names, is_child)
        if result is None and path.startswith("/Script/"):
            result = rp.verified_supers(path, ancestors, is_child)
        return result
    except Exception:
        return None


def _paks_dir():
    """The cooked content folder of this editor (<Project>/Content/Paks), absolute."""
    content = unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir())
    return os.path.join(str(content), "Paks")


def _member_candidates(loaded, wanted, errors):
    """Candidate member names of the requested /Game classes `wanted` -> ({requested path: {lower: (name, kind)}}, sources).
    Sources: the widget tree and SCS template objects the engine has loaded ("widgetTree", "scs"; kind widget / component) and the
    names the class's cooked package spells ("cookedClass": name map, export names; designer widgets are the exports under the
    WidgetTree export, SCS components the `<Var>_GEN_VARIABLE` exports; every other name is kind member). These are only candidates:
    the caller checks each one on the class default object. A failing source is skipped and recorded in `errors`."""
    cand, sources = {}, []
    if not wanted:
        return cand, sources
    by_path = {loaded[p][1]: p for p in wanted}
    widgets, scs = {}, {}
    for what, cls_ in (("widgetTree", unreal.Widget), ("scs", unreal.ActorComponent)):
        try:
            paths = []
            for o in unreal.ObjectIterator(cls_):
                try:
                    paths.append(o.get_path_name())
                except Exception:
                    continue
            w, s = rp.group_member_paths(paths, set(by_path))
            if what == "widgetTree":
                widgets = w
            else:
                scs = s
            sources.append(what)
        except Exception as e:
            errors.append("member discovery: %s objects could not be listed (%s)" % (what, str(e)[:200]))
    cooked, index = {}, None
    try:
        index = cookedpkg.CookedIndex(_paks_dir())
    except Exception as e:
        errors.append("member discovery: cooked package reader unavailable (%s)" % str(e)[:200])
    if index is not None:
        if index.rejected():
            errors.append("member discovery: cooked container(s) skipped (%s)" % "; ".join("%s: %s" % s for s in index.rejected())[:300])
        for p in wanted:
            try:
                got = index.class_members(loaded[p][1])
            except Exception as e:
                errors.append("member discovery: %s could not be read from the cooked packages (%s)" % (p, str(e)[:200]))
                continue
            if got is None:
                errors.append("member discovery: %s not found in the cooked packages" % p)
                continue
            cooked[p] = got
        if cooked:
            sources.append("cookedClass")
    empty = {"names": set(), "base": set(), "widgets": set(), "components": set(), "timelines": set()}
    for p in wanted:
        pn = loaded[p][1]
        c = dict(empty, **cooked.get(p, {}))
        # a numbered variable (NewVar_0) is spelled `NewVar` in the name map: probe the base names with a number appended
        numbered = cookedpkg.numbered_names(n for n in c["base"] if rp.member_name_ok(n))
        cand[p] = rp.member_candidates(widgets.get(pn, set()) | c["widgets"], scs.get(pn, set()) | c["components"] | c["timelines"],
                                       c["names"] | numbered)
    return cand, sources


def _own_members(cls, cand, parent_cls):
    """[(name, kind)] of the candidates `cand` ({lower: (name, kind)}) that are properties of the class default object of `cls`
    (get_editor_property is the exact existence check: it raises for unknown names and for functions), can hold an object
    reference (the value is unset or an object) and are NOT properties of the parent's default object (declared on this class).
    Must run before the first scratch-Blueprint template run: reading an inherited multicast delegate on a child default object
    afterwards crashes the commandlet."""
    cdo = unreal.get_default_object(cls)
    pget = unreal.get_default_object(parent_cls).get_editor_property if parent_cls is not None else None
    out = []
    for _low, (name, kind) in cand.items():
        ok, value = rp.probe_property(cdo.get_editor_property, name)
        if not ok or not rp.is_object_value(value, unreal.Object):
            continue
        if pget is not None and rp.has_property(pget, name):
            continue
        out.append((name, kind))
    return out


def _doc_members(cls, parent_cls):
    """Native classes: [PascalCase name] of the editor properties in the Python docstring of the class default object's wrapper
    (it lists inherited ones too) that the parent's docstring does not list and that can hold an object reference. The names are
    typed by a VariableGet node later, which resolves them case-insensitively."""
    cdo = unreal.get_default_object(cls)
    names = rp.doc_property_names(type(cdo).__doc__)
    inherited = set(rp.doc_property_names(type(unreal.get_default_object(parent_cls)).__doc__)) if parent_cls is not None else set()
    out = []
    for snake in names:
        if snake in inherited:
            continue
        ok, value = rp.probe_property(cdo.get_editor_property, snake)
        if ok and rp.is_object_value(value, unreal.Object):
            out.append(rp.snake_to_pascal(snake))
    return out


INSTANCED_TYPES = (("timeline", "/Script/Engine.TimelineComponent"), ("component", "/Script/Engine.ActorComponent"),
                   ("visual", "/Script/UMG.Visual"))


def _instanced_type(pin):
    """"timeline", "component" or "visual" when the object type of a VariableGet output pin is a TimelineComponent / other ActorComponent /
    Visual (widget) class, else None. These are the DefaultToInstanced types whose single references GetClassDefaults cannot list; a
    discovered member of any other type is not Blueprint-visible (GetClassDefaults would have listed it) and is dropped."""
    try:
        sub = unreal.load_class(None, pin["subCategoryObject"])
        if sub is None:
            return None
        for kind, path in INSTANCED_TYPES:
            base = unreal.load_class(None, path)
            if base is not None and unreal.MathLibrary.class_is_child_of(sub, base):
                return kind
    except Exception:
        pass
    return None


def _member_pass(loaded, classes_in, result_classes, result_supers, errors):
    """Everything about members that needs no scratch Blueprint ({requested path: [(name, kind)]}, member sources). Kind
    "native" (a name from a native class's docstring) is resolved from the pin type once the name has been typed. Never raises."""
    own, sources = {}, []
    try:
        wanted = [p for p in classes_in if p in loaded and loaded[p][1].startswith("/Game/")]
        natives = [p for p in classes_in if p in loaded and loaded[p][1].startswith("/Script/")]
        classes = {}

        def load(path):
            if path not in classes:
                classes[path] = unreal.load_class(None, path)
            return classes[path]

        def parent_of(p, strict):
            """(parent class or None, resolved): resolved is False when the parent should exist but cannot be found."""
            pname = loaded[p][1]
            sup = result_supers.get(p)
            if sup:
                path = sup[0]
            elif strict or pname == rp.OBJECT_PATH:
                return None, pname == rp.OBJECT_PATH
            else:
                path = next((a for a in result_classes.get(p, {}).get("ancestors", []) if a != pname), None)
                if path is None:
                    return None, False
            par = load(path)
            return par, par is not None

        try:
            cand, sources = _member_candidates(loaded, wanted, errors)
        except Exception as e:
            cand, wanted = {}, []
            errors.append("member discovery: candidates failed (%s)" % str(e)[:200])
        for p in wanted:
            try:
                par, ok = parent_of(p, True)
                if not ok or p not in cand:
                    errors.append("member discovery: %s skipped (parent class unknown)" % p)
                    continue
                own[p] = _own_members(loaded[p][0], cand[p], par)
            except Exception as e:
                errors.append("member discovery: %s failed (%s)" % (p, str(e)[:200]))
        doc_ran = False
        for p in natives:
            try:
                par, ok = parent_of(p, False)
                if not ok:
                    errors.append("member discovery: %s skipped (parent class unknown)" % p)
                    continue
                own[p] = [(n, "native") for n in _doc_members(loaded[p][0], par)]
                doc_ran = True
            except Exception as e:
                errors.append("member discovery: %s failed (%s)" % (p, str(e)[:200]))
        if doc_ran:
            sources.append("pythonDoc")
    except Exception as e:
        errors.append("member discovery failed (%s)" % str(e)[:200])
    return own, sources


def run(job):
    errors = []
    classes_in = list(job.get("classes") or [])
    structs_in = list(job.get("structs") or [])
    casts_in = list(job.get("cast_targets") or [])
    supers_in = list(job.get("supers_of") or [])
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

    func_objs = {}
    funcs_by_outer = _function_index({v[1] for k, v in loaded.items() if k in classes_in}, func_objs)

    result_classes = {}
    call_items, key_info = {}, {}
    meta_items = []   # [((class path, function name), UFunction)]
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
            if (pname, n) in func_objs:
                meta_items.append(((p, n), func_objs[(pname, n)]))
        defaults_items["d%d" % len(defaults_items)] = (DEFAULTS, [rp.class_pin_line(pname, meta)])
        key_info["d%d" % (len(defaults_items) - 1)] = (p, "defaults", None)

    # parent chains (Blueprint and native) of every loaded class and of the supers_of paths; absent key = unknown
    result_supers = {}
    for p in loaded:
        s = _supers(loaded[p][0], result_classes.get(p, {}).get("ancestors") or _ancestors(loaded[p][0]))
        if s is not None:
            result_supers[p] = s
    for p in supers_in:
        if p in loaded:
            continue
        try:
            cls = unreal.load_class(None, _load_path(p))
            if cls is None:
                continue
            s = _supers(cls, _ancestors(cls))
        except Exception:
            continue
        if s is not None:
            result_supers[p] = s

    # members that GetClassDefaults cannot list (single references to instanced classes: widgets, components, widget/component
    # variables): checked on the default objects now, BEFORE the first scratch-Blueprint template run (see _own_members)
    own_members, member_sources = _member_pass(loaded, classes_in, result_classes, result_supers, errors)

    call_pins = _templates(call_items, errors, "function")

    # properties: the output pins of a GetClassDefaults node name the readable properties; one VariableGet each gives the pins
    get_items = {}
    known = {p: {n.lower() for n in funcs_by_outer.get(loaded[p][1], [])} for p in result_classes if p in loaded}
    for key, pins in _templates(defaults_items, errors, "class defaults").items():
        p = key_info[key][0]
        _cls, pname, meta = loaded[p]
        ct = _class_text(pname, meta)
        for pin in pins:
            if pin["dir"] == "out" and pin["category"] != "exec" and not pin["hidden"]:
                gk = "p%d" % len(get_items)
                get_items[gk] = (GET, rp.get_body(ct, pin["name"]))
                key_info[gk] = (p, "properties", pin["name"])
                known.setdefault(p, set()).add(pin["name"].lower())
    member_kind = {}
    for p, mems in own_members.items():
        _cls, pname, meta = loaded[p]
        ct = _class_text(pname, meta)
        for n, kind in mems:
            if n.lower() in known.get(p, ()):
                continue
            gk = "p%d" % len(get_items)
            get_items[gk] = (GET, rp.get_body(ct, n))
            key_info[gk] = (p, "member", n)
            member_kind[(p, n)] = kind
    have = {}   # lowercase property names already listed per class
    for key, pins in _templates(get_items, errors, "property").items():
        p, slot, n = key_info[key]
        if slot == "member":
            pin = rp.member_pin(pins, n)
            if pin is None or pin["name"].lower() in have.setdefault(p, set()):
                continue
            itype = _instanced_type(pin)
            if itype is None:
                continue   # not a widget / component type: a Blueprint-visible one would be in GetClassDefaults, so this is editor-only
            kind = member_kind[(p, n)]
            if itype == "timeline":
                kind = "component"      # no Set node for a timeline variable
            elif kind == "native":
                kind = "component" if itype == "component" else "member"
            have[p].add(pin["name"].lower())
            result_classes[p]["properties"].append({"name": pin["name"], "pins": pins, "kind": kind})
            continue
        have.setdefault(p, set()).add(n.lower())
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

    # function metadata (tooltip, display name) from the UFunction's metadata tags, read after every template run (a crash seen
    # once during the function-template chunk is worked around by this order); None where unreadable, over the time cap or skipped
    want_meta = job.get("meta", True) is not False
    if want_meta:
        pin_names = {}
        for key, pins in call_pins.items():
            pin_names[(key_info[key][0], key_info[key][2])] = [x["name"] for x in pins if x["dir"] == "in" and x["category"] != "exec"]
        metas = rp.collect_function_meta(lambda f, tag: unreal.EditorAssetLibrary.get_metadata_tag(f, tag), meta_items,
                                         read_all=lambda f: unreal.EditorAssetLibrary.get_metadata_tag_values(f), pins=pin_names)
    else:
        metas = {}
    for key, pins in call_pins.items():
        p, _slot, n = key_info[key]
        result_classes[p]["functions"].append({"name": n, "pins": pins, "meta": metas.get((p, n))})

    out = {"engineVersion": unreal.SystemLibrary.get_engine_version(), "meta": "read" if want_meta else "skipped",
           "classes": result_classes, "structs": result_structs, "supers": result_supers, "memberSources": member_sources,
           "errors": errors}
    with open(os.path.join(job["run"], "reflect.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)
    counts = {"classes": len(result_classes),
              "functions": sum(len(c["functions"]) for c in result_classes.values()),
              "properties": sum(len(c["properties"]) for c in result_classes.values()),
              "members": sum(1 for c in result_classes.values() for x in c["properties"] if "kind" in x),
              "structs": len(result_structs), "supers": len(result_supers), "errors": len(errors)}
    if job.get("gameplay_tags"):
        from . import tags as tags_mod
        out["gameplayTags"] = tags_mod.list_tags()
        counts["gameplayTags"] = len(out["gameplayTags"]["tags"])
        with open(os.path.join(job["run"], "reflect.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, default=str)
    return {"file": "reflect.json", "counts": counts, "meta": out["meta"]}
