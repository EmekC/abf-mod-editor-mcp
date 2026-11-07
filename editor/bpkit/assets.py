"""Editor-side asset operations: create, variables, components, dispatchers, macros, interfaces, timelines, widget tree,
widget animations. Every load result is guarded for None."""
import ctypes
import re
import struct

import unreal

from . import core, t3d
from .core import CFG, LIB, must, pth, addr, rd

FACTORIES = {"Blueprint": ("BlueprintFactory", "Blueprint", "/Script/Engine.Actor"),
             "WidgetBlueprint": ("WidgetBlueprintFactory", "WidgetBlueprint", "/Script/UMG.UserWidget"),
             "BlueprintInterface": ("BlueprintInterfaceFactory", "Blueprint", "/Script/CoreUObject.Object"),
             "UserDefinedStruct": ("StructureFactory", "UserDefinedStruct", None)}


def new_asset(path, create):
    """create = {"class": Blueprint|WidgetBlueprint|BlueprintInterface|UserDefinedStruct, "parent": class path, "if_missing": bool}."""
    kind = create["class"]
    if kind not in FACTORIES:
        raise RuntimeError("cannot create a %s" % kind)
    fac_name, asset_cls, default_parent = FACTORIES[kind]
    fac = getattr(unreal, fac_name)()
    if kind not in ("BlueprintInterface", "UserDefinedStruct"):
        parent = must(unreal.load_class(None, create.get("parent") or default_parent), "parent class " + str(create.get("parent")))
        fac.set_editor_property("parent_class", parent)
    folder, name = path.rsplit("/", 1)
    return must(core.tools().create_asset(name, folder, getattr(unreal, asset_cls), fac), "new asset " + path)


# ---- variables ---------------------------------------------------------------

def variable_lines(bp):
    """Values of the NewVariables(i)= lines in the Blueprint's export."""
    txt = core.export_text(bp, "bk_vars.t3d")
    return [l.strip()[l.strip().index("=") + 1:].rstrip("\r") for l in txt.split("\n") if l.startswith("   NewVariables(")]


def _var_name(v):
    m = re.match(r'\(VarName="([^"]+)"', v)
    return m.group(1) if m else None


def set_vars(bp, add_lines, remove=()):
    """Rewrite NewVariables from the export: drop `remove`, append `add_lines`. Duplicate names raise."""
    off = core.find_vars_off(bp)
    cur = variable_lines(bp)
    gone = set(remove)
    missing = gone - {_var_name(v) for v in cur}
    if missing:
        raise RuntimeError("cannot remove unknown variable(s): %s" % sorted(missing))
    cur = [v for v in cur if _var_name(v) not in gone]
    names = [_var_name(v) for v in cur]
    for v in add_lines:
        n = _var_name(v)
        if n in names:
            raise RuntimeError("variable %s already exists" % n)
        names.append(n)
    all_v = cur + list(add_lines)
    ls = "".join("      NewVariables(%d)=%s\n" % (i, v) for i, v in enumerate(all_v))
    ib = core.carry('   Begin Object Class=%s Name="@@NAME@@"\n%s   End Object' % (core.bpcls(bp), ls), "BkVars", unreal.Blueprint)
    _p, c, _m = struct.unpack_from("<Qii", rd(addr(ib) + off, 16))
    if c != len(all_v):
        raise RuntimeError("variable text not imported: %d of %d" % (c, len(all_v)))
    core.swap(addr(ib) + off, addr(bp) + off, 16)
    return len(all_v)


# ---- components ----------------------------------------------------------------

def _subsystem():
    return unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)


def _handles(ss, bp):
    return list(ss.k2_gather_subobject_data_for_blueprint(bp))


def _handle_name(ss, h):
    d = ss.k2_find_subobject_data_from_handle(h)
    if d is None:
        return None
    return str(unreal.SubobjectDataBlueprintFunctionLibrary.get_variable_name(d))


def _component_object(ss, h, bp):
    d = ss.k2_find_subobject_data_from_handle(h)
    return must(unreal.SubobjectDataBlueprintFunctionLibrary.get_object_for_blueprint(must(d, "subobject data"), bp), "component object")


def add_components(bp, items):
    """items: {"name","class","parent","properties","expressions","remove"}. No class on an existing name modifies it."""
    ss = _subsystem()
    report = []
    for it in items:
        name = it["name"]
        hs = _handles(ss, bp)
        found = [h for h in hs[1:] if _handle_name(ss, h) == name]
        if it.get("remove"):
            if not found:
                raise RuntimeError("cannot remove unknown component %s" % name)
            ss.delete_subobject(hs[0], found[0], bp)
            report.append({"name": name, "removed": True})
            continue
        if found and not it.get("class"):
            h = found[0]
        elif found:
            raise RuntimeError("component %s already exists; leave out 'class' to modify it" % name)
        else:
            if not it.get("class"):
                raise RuntimeError("new component %s needs a class" % name)
            cls = must(unreal.load_class(None, it["class"]), "component class " + it["class"])
            if it.get("parent"):
                ph = [x for x in hs if _handle_name(ss, x) == it["parent"]]
                if not ph:
                    raise RuntimeError("parent component %s not found" % it["parent"])
                parent = ph[0]
            else:
                parent = hs[1] if len(hs) > 1 else hs[0]
            p = unreal.AddNewSubobjectParams()
            p.set_editor_property("parent_handle", parent)
            p.set_editor_property("new_class", cls)
            p.set_editor_property("blueprint_context", bp)
            h, fail = ss.add_new_subobject(p)
            if fail is not None and str(fail):
                raise RuntimeError("add component %s failed: %s" % (name, fail))
            ss.rename_subobject(h, unreal.Text(name))
            if _handle_name(ss, h) != name:
                raise RuntimeError("component rename to %s did not stick (%s)" % (name, _handle_name(ss, h)))
        obj = _component_object(ss, h, bp)
        for k, v in (it.get("properties") or {}).items():
            obj.set_editor_property(k, v)
        for k, expr in (it.get("expressions") or {}).items():
            obj.set_editor_property(k, eval(expr, {"unreal": unreal}, {}))
        report.append({"name": name, "class": it.get("class")})
    return report


def component_vars(bp):
    """The Blueprint's components as object variables (for the scratch Blueprint)."""
    ss = _subsystem()
    out = []
    for h in _handles(ss, bp)[1:]:
        name = _handle_name(ss, h)
        d = ss.k2_find_subobject_data_from_handle(h)
        obj = unreal.SubobjectDataBlueprintFunctionLibrary.get_object_for_blueprint(d, bp) if d is not None else None
        if name and obj is not None:
            out.append(t3d.var_line(name, "object:" + pth(obj.get_class())))
    return out


# ---- dispatchers, macros, interfaces, timelines -------------------------------------

def add_dispatcher(bp, name, params):
    var, gtext = t3d.dispatcher_text(name, params)
    set_vars(bp, [var])
    elem = "\"/Script/Engine.EdGraph'BkG0'\""
    off = core.bp_arr_off(bp, "DelegateSignatureGraphs", elem, '      Begin Object Class=/Script/Engine.EdGraph Name="BkG0"\n      End Object\n')
    ib = core.array_append(bp, "DelegateSignatureGraphs", off, gtext, ["\"/Script/Engine.EdGraph'%s'\"" % name])
    return core.adopt_graphs(ib, [name], bp)


def add_macro(bp, name, defs, guid):
    elem = "\"/Script/Engine.EdGraph'BkG0'\""
    off = core.bp_arr_off(bp, "MacroGraphs", elem, '      Begin Object Class=/Script/Engine.EdGraph Name="BkG0"\n      End Object\n')
    ib = core.array_append(bp, "MacroGraphs", off, t3d.graph_obj(name, defs, guid=guid), ["\"/Script/Engine.EdGraph'%s'\"" % name])
    return core.adopt_graphs(ib, [name], bp)


def add_interface(bp, iface_class, graphs_text, names):
    """Append an ImplementedInterfaces entry (FBPInterfaceDescription: 8 + 16 bytes) keeping the existing ones."""
    ie = "(Interface=\"/Script/Engine.BlueprintGeneratedClass'%s'\")" % iface_class
    off = core.bp_arr_off(bp, "ImplementedInterfaces", ie)
    gl = ",".join("\"/Script/Engine.EdGraph'%s'\"" % n for n in names)
    el = "(Interface=\"/Script/Engine.BlueprintGeneratedClass'%s'\",Graphs=(%s))" % (iface_class, gl)
    ib = core.array_append(bp, "ImplementedInterfaces", off, graphs_text, [el], stride=24)
    return core.adopt_graphs(ib, names, bp)


def timeline_guids(bp):
    """{timeline name: guid} of the templates in the Blueprint's export."""
    out, cur = {}, None
    for l in core.export_text(bp, "bk_tl.t3d").split("\n"):
        m = re.match(r'\s*Begin Object Name="(.+)_Template"', l)
        if m:
            cur = m.group(1)
            continue
        g = re.match(r"\s*TimelineGuid=([0-9A-Fa-f]{32})", l)
        if g and cur:
            out[cur] = g.group(1).upper()
    return out


def add_timeline(bp, spec, guid=None):
    text, tn, guid = t3d.timeline_text(spec, guid)
    elem = "\"/Script/Engine.TimelineTemplate'BkT'\""
    off = core.bp_arr_off(bp, "Timelines", elem, '      Begin Object Class=/Script/Engine.TimelineTemplate Name="BkT"\n      End Object\n')
    ib = core.array_append(bp, "Timelines", off, text, ["\"/Script/Engine.TimelineTemplate'%s'\"" % tn])
    pre = pth(ib) + "."
    tpl = [o for o in unreal.ObjectIterator(unreal.TimelineTemplate) if pth(o).startswith(pre) and o.get_name() == tn]
    if not tpl:
        raise RuntimeError("timeline template %s not found after import" % tn)
    tpl[0].rename(None, must(LIB.generated_class(bp), "generated class"))
    return guid


# ---- widgets ---------------------------------------------------------------------

def tree_widgets(tree):
    p = pth(tree) + "."
    return [w for w in unreal.ObjectIterator(unreal.Widget) if pth(w).startswith(p) and "." not in pth(w)[len(p):]]


def set_widget_tree(wbp, tree_text, root_name):
    """Replace the layout: widgets move into the real tree and RootWidget is repointed.
    (Renaming an old WidgetTree leaves a redirector that keeps the name, so the tree object itself stays.)"""
    txt = '   Begin Object Class=/Script/UMG.WidgetTree Name="@@NAME@@"\n' + tree_text + "\n   End Object"
    nt = core.carry(txt, "BkTree", unreal.Object)
    real = must(unreal.find_object(None, pth(wbp) + ":WidgetTree"), "widget tree of " + pth(wbp))
    new = tree_widgets(nt)
    root = [w for w in new if w.get_name() == root_name]
    if not root:
        raise RuntimeError("root widget not imported")
    want = struct.pack("<Q", addr(root[0]))
    b = rd(addr(nt), 0x100)
    hits = [o for o in range(0x28, 0xf8, 8) if b[o:o + 8] == want]
    if len(hits) != 1:
        raise RuntimeError("RootWidget slot ambiguous %s" % hits)
    old = tree_widgets(real)
    tr = must(unreal.find_object(None, "/Engine/Transient"), "transient package")
    for w in old:
        w.rename("BkOldW_" + t3d.gid()[:8], tr)
    for w in new:
        if unreal.find_object(None, pth(real) + "." + w.get_name()) is not None:
            raise RuntimeError("name taken: " + w.get_name())
    ctypes.memmove(addr(real) + hits[0], want, 8)
    for w in new:
        w.rename(None, real)
    return {"root_off": hits[0], "old": len(old), "new": len(new)}


def tree_replace(wbp, root, replacements):
    text = t3d.tree_block(core.export_text(wbp, "bk_tree.t3d"), wbp.get_name())
    for old, new in replacements:
        if old not in text:
            raise RuntimeError("tree_replace: %r not found in the widget tree text" % old)
        text = text.replace(old, new)
    return set_widget_tree(wbp, text, root)


def widget_classes(wbp):
    real = must(unreal.find_object(None, pth(wbp) + ":WidgetTree"), "widget tree")
    return {w.get_name(): pth(w.get_class()) for w in tree_widgets(real)}


def widget_vars(wbp):
    return [t3d.var_line(n, "object:" + c) for n, c in widget_classes(wbp).items()]


def add_animation(wbp, spec):
    """spec: {"name","fps","end_frame","tracks":[{"widget","property","keys":[[frame,value],...]}]}."""
    name = spec["name"]
    classes = widget_classes(wbp)
    text = t3d.animation_text(spec, classes)
    elem = "\"/Script/UMG.WidgetAnimation'BkA'\""
    off = core.bp_arr_off(wbp, "Animations", elem, '      Begin Object Class=/Script/UMG.WidgetAnimation Name="BkA"\n      End Object\n')
    ib = core.array_append(wbp, "Animations", off, text, ["\"/Script/UMG.WidgetAnimation'%s'\"" % name])
    pre = pth(ib) + "."
    found = [o for o in unreal.ObjectIterator(unreal.WidgetAnimation) if pth(o).startswith(pre) and o.get_name() == name]
    if not found:
        raise RuntimeError("animation %s not found after import" % name)
    a = found[0]
    a.rename(None, wbp)
    end = int(spec.get("end_frame", 20))
    a.set_display_rate(unreal.FrameRate(int(spec.get("fps", 20)), 1))
    a.set_playback_start(0)
    a.set_playback_end(end)
    order = []
    for t in spec["tracks"]:
        if t["widget"] not in order:
            order.append(t["widget"])
    bindings = list(a.get_bindings())
    by_name = {}
    for i, b in enumerate(bindings):
        by_name.setdefault(str(b.get_display_name()), b)
    for t in spec["tracks"]:
        b = by_name.get(t["widget"])
        if b is None:
            idx = order.index(t["widget"])
            if idx >= len(bindings):
                raise RuntimeError("no animation binding for widget %s" % t["widget"])
            b = bindings[idx]
        tr = must(b.add_track(unreal.MovieSceneFloatTrack), "float track")
        tr.set_property_name_and_path(t["property"], t["property"])
        sec = must(tr.add_section(), "track section")
        sec.set_range(0, end)
        ch = sec.get_all_channels()[0]
        for frame, value in t["keys"]:
            ch.add_key(unreal.FrameNumber(int(frame)), float(value))
    return a
