"""Interpreter for one bp_apply asset spec. Everything stays in memory; saving is the caller's decision."""
import re

import unreal

from . import assets, core, graphs, specs, t3d
from .core import LIB, must, pth
from .t3d import SpecError

BP_CLASSES = ("Blueprint", "WidgetBlueprint")
FINAL_MARK = "ABFMCP_APPLY_FINAL"   # headless.final_compile_errors reads the log after the last one of an asset


class Env:
    """What node kind functions need: class references and timeline guids for this asset."""

    def __init__(self, bp):
        self.bp = bp
        self.timeline_guids = {}

    def class_text(self, path):
        if "'" in path:
            return path
        if path.startswith("/Script/"):
            return t3d.class_ref(path)
        last = path.rsplit("/", 1)[-1]
        if "." not in last:
            path = "%s.%s_C" % (path, last)
        cls = must(unreal.load_class(None, path), "class " + path)
        meta = {"WidgetBlueprintGeneratedClass": "/Script/UMG.WidgetBlueprintGeneratedClass"}.get(
            cls.get_class().get_name(), "/Script/Engine.BlueprintGeneratedClass")
        return t3d.class_ref(path, meta)

    def self_class_text(self):
        return core.gen_class_text(self.bp)

    def timeline_guid(self, name):
        if name not in self.timeline_guids:
            raise SpecError("timeline %s is not defined in this batch (add it under 'timelines')" % name)
        return self.timeline_guids[name]


def _resolve(spec, mod):
    path = spec["asset"]
    name = path.rsplit("/", 1)[-1]
    create = spec.get("create")
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    ad = reg.get_asset_by_object_path("%s.%s" % (path, name))
    exists = bool(ad.is_valid())
    if exists:
        cls = str(ad.asset_class_path.asset_name)
        if cls == "AnimSequence" or cls.startswith("AnimSequence"):
            raise RuntimeError("%s is an AnimSequence; cooked animations are never loaded" % path)
        if cls not in BP_CLASSES + ("AnimBlueprint",):
            raise RuntimeError("%s is a %s, not a Blueprint" % (path, cls))
    if create:
        if exists:
            if not create.get("if_missing"):
                raise RuntimeError("%s already exists; pass create.if_missing to edit it" % path)
        else:
            return assets.new_asset(path, create), True
    elif not exists:
        raise RuntimeError("%s does not exist (give 'create' to make it)" % path)
    return must(unreal.load_asset(path), "asset " + path), False


def _scopes(spec):
    """(scope, kind, owner, nodes, wires) for every node-bearing graph spec."""
    out = []
    for m in spec.get("macros") or []:
        out.append(("macro:" + m["name"], "macro", m, m.get("nodes") or [], m.get("wires") or []))
    for f in spec.get("functions") or []:
        out.append(("fn:" + f["name"], "function", f, f.get("nodes") or [], f.get("wires") or []))
    for i, it in enumerate(spec.get("interfaces") or []):
        for f in it.get("functions") or []:
            out.append(("if:%d:%s" % (i, f["name"]), "interface", f, f.get("nodes") or [], f.get("wires") or []))
    for i, g in enumerate(spec.get("graphs") or []):
        out.append(("graph:%d" % i, "graph", g, g.get("add") or [], g.get("wires") or []))
    return out


_MEMBER_GUID = re.compile(r",?MemberGuid=[0-9A-Fa-f]+")


def _self_events(scopes):
    """{name: params} of every custom event the spec adds, over all scopes."""
    out = {}
    for _scope, _kind, _owner, nodes, _w in scopes:
        for n in nodes:
            if n.get("k") == "event" and n.get("name"):
                out[n["name"]] = n.get("params") or []
    return out


def _fn_nodes(f, parent=None, editable=True):
    ins, outs = f.get("inputs") or [], f.get("outputs") or []
    nodes = [t3d.entry_node(f["name"], ins, parent=parent, editable=editable)]
    if outs:
        nodes.append(t3d.result_node(f["name"], outs, parent=parent))
    return nodes


def _preload(env, node):
    """Load every class a node names before any text is imported: asset refs in pins resolve only if the class is loaded."""
    for key in ("class", "target", "interface"):
        v = node.get(key)
        if isinstance(v, str) and v.startswith("/") and not v.startswith("/Script/") and "'" not in v:
            env.class_text(v)
    for v in (node.get("objects") or {}).values():
        if isinstance(v, str) and v.startswith("/") and not v.startswith("/Script/") and "'" not in v:
            env.class_text(v)


def apply_asset(spec, mod, index=0):
    path = spec["asset"]
    report = {"asset": path, "created": False, "node_ids": {}, "nodes": 0, "links": 0}
    if (spec.get("create") or {}).get("class") == "UserDefinedStruct":
        # no Blueprint compile, no export of nodes; the object is None when an identical struct already exists
        from . import structs
        if unreal.EditorAssetLibrary.does_asset_exist(path) and not spec["create"].get("if_missing"):
            raise RuntimeError("%s already exists; pass create.if_missing to check it" % path)
        obj, info = structs.set_fields(path, spec["struct_fields"])
        report.update(info)
        report["created"] = obj is not None
        if obj is not None:
            report["object_path"] = pth(obj)
        return report, obj
    bp, created = _resolve(spec, mod)
    report["created"] = created
    env = Env(bp)
    is_wbp = bp.get_class().get_name() == "WidgetBlueprint"

    # 2. variables
    adds = [t3d.var_line(v["name"], v["type"], None, v.get("default"), int(v.get("flags", 5))) for v in spec.get("variables") or []]
    rm = spec.get("remove_variables") or []
    if adds or rm:
        assets.set_vars(bp, adds, rm)
        LIB.compile_blueprint(bp)

    # 3. components
    if spec.get("components"):
        report["components"] = assets.add_components(bp, spec["components"])
        LIB.compile_blueprint(bp)

    # 4. dispatchers, timelines, widget tree, animations
    for d in spec.get("dispatchers") or []:
        assets.add_dispatcher(bp, d["name"], d.get("params") or [])
    if spec.get("dispatchers"):
        LIB.compile_blueprint(bp)
    tl_specs = {}
    for t in spec.get("timelines") or []:
        guid = assets.add_timeline(bp, t)
        env.timeline_guids[t["name"]] = guid
        tl_specs[t["name"]] = (t, guid)
    if spec.get("timelines"):
        LIB.compile_blueprint(bp)
    if spec.get("widget_tree"):
        if not is_wbp:
            raise RuntimeError("widget_tree needs a WidgetBlueprint")
        text, root = t3d.widget_tree_text(spec["widget_tree"]["root"])
        assets.set_widget_tree(bp, text, root)
        LIB.compile_blueprint(bp)
    for a in spec.get("animations") or []:
        if not is_wbp:
            raise RuntimeError("animations need a WidgetBlueprint")
        assets.add_animation(bp, a)
    if spec.get("animations"):
        LIB.compile_blueprint(bp)

    # 5. functions: signatures first (entry and result only)
    for name in spec.get("remove_functions") or []:
        must(LIB.find_graph(bp, name), "function graph " + name)
        LIB.remove_function_graph(bp, name)
    for f in spec.get("functions") or []:
        g = must(LIB.add_function_graph(bp, f["name"]), "new function graph " + f["name"])
        graphs.write_graph(g, [n.defn() for n in _fn_nodes(f)])
    if spec.get("functions") or spec.get("remove_functions"):
        LIB.compile_blueprint(bp)

    # 6. templates for every engine-made node
    scopes = _scopes(spec)
    for _scope, _kind, owner, nodes, _w in scopes:
        for n in nodes:
            _preload(env, n)
        if _kind == "graph":
            _preload(env, owner)
    items, keymap, extra_vars, disp = {}, {}, [], {}
    self_events, self_keys = _self_events(scopes), set()
    for d in spec.get("dispatchers") or []:
        disp[d["name"]] = d.get("params") or []
    for scope, _kind, _owner, nodes, _w in scopes:
        for n in nodes:
            k = n["k"]
            if k in specs.MANUAL_KINDS:
                continue
            cls, body = specs.NODE_KINDS[k](n, env)
            key = "n%d" % len(items)
            if k == "call" and not n.get("class") and n["function"] in self_events:
                # the real class lacks this event until the edit below: template it against the scratch class's own event
                body = ['FunctionReference=(MemberName="%s",bSelfContext=True)' % n["function"]]
                self_keys.add(key)
            items[key] = (cls, body)
            keymap[(scope, n["id"])] = key
            if k in ("get", "set") and n.get("type"):
                extra_vars.append(t3d.var_line(n["name"], n["type"]))
            if k in ("call_dispatcher", "bind_dispatcher") and n["name"] not in disp:
                disp[n["name"]] = n.get("params") or []
    templates = {}
    if items:
        seen, svars = set(), []
        lines = assets.variable_lines(bp) + assets.component_vars(bp) + (assets.widget_vars(bp) if is_wbp else []) + extra_vars
        for v in lines:
            vn = re.match(r'\(VarName="([^"]+)"', v)
            if not vn or vn.group(1) in seen or 'PinCategory="mcdelegate"' in v:
                continue
            seen.add(vn.group(1))
            svars.append(v)
        setups = [(lambda z, n=n, p=p: assets.add_dispatcher(z, n, p)) for n, p in disp.items()]
        setups += [(lambda z, t=t, g=g: assets.add_timeline(z, t, g)) for t, g in tl_specs.values()]
        raw = graphs.make_templates(items, svars, setups, list(self_events.items()))
        cls_text = core.gen_class_text(bp)
        for sk, key in keymap.items():
            templates[sk] = t3d.retarget(raw[key], cls_text)
            if key in self_keys:
                # the scratch event's guid means nothing on the real class
                templates[sk] = [_MEMBER_GUID.sub("", l) if l.startswith("FunctionReference=") else l for l in templates[sk]]
    ctx = {"env": env, "macros": {}, "bp_path": pth(bp), "scope": None}

    def run_edit(scope, graph, edit):
        ctx["scope"] = scope
        ids = graphs.edit_graph(bp, graph, edit, templates, ctx)
        report["node_ids"][scope] = ids
        LIB.compile_blueprint(bp)

    # 7. macros
    for m in spec.get("macros") or []:
        ins, outs = m.get("inputs") or [], m.get("outputs") or []
        a, b = t3d.tunnel_nodes(ins, outs)
        guid = t3d.gid()
        assets.add_macro(bp, m["name"], [a.defn(), b.defn()], guid)
        ctx["macros"][m["name"]] = (guid, ins, outs)
        LIB.compile_blueprint(bp)
        if m.get("nodes") or m.get("wires"):
            run_edit("macro:" + m["name"], must(LIB.find_graph(bp, m["name"]), "macro graph " + m["name"]),
                     {"add": m.get("nodes") or [], "wires": m.get("wires") or []})

    # 8. interfaces
    for i, it in enumerate(spec.get("interfaces") or []):
        icls = env.class_text(it["class"])
        parent = icls
        defs_text, names = "", []
        for f in it.get("functions") or []:
            defs = [n.defn() for n in _fn_nodes(f, parent=parent, editable=False)]
            defs_text += t3d.graph_obj(f["name"], defs)
            names.append(f["name"])
        if names:
            assets.add_interface(bp, it["class"].split("'")[-2] if "'" in it["class"] else it["class"], defs_text, names)
            LIB.compile_blueprint(bp)

    # 9. function bodies, then graphs
    for f in spec.get("functions") or []:
        if f.get("nodes") or f.get("wires"):
            run_edit("fn:" + f["name"], must(LIB.find_graph(bp, f["name"]), "function graph " + f["name"]),
                     {"add": f.get("nodes") or [], "wires": f.get("wires") or []})
    for i, it in enumerate(spec.get("interfaces") or []):
        for f in it.get("functions") or []:
            if f.get("nodes") or f.get("wires"):
                run_edit("if:%d:%s" % (i, f["name"]), must(LIB.find_graph(bp, f["name"]), "interface function graph " + f["name"]),
                         {"add": f.get("nodes") or [], "wires": f.get("wires") or []})
    for i, g in enumerate(spec.get("graphs") or []):
        run_edit("graph:%d" % i, graphs.find_graph(bp, g["graph"]), g)

    # 10. raw
    for r in spec.get("raw") or []:
        kind = r["kind"]
        if kind == "graph":
            graph = graphs.find_graph(bp, r["graph"])
            graphs.write_graph(graph, t3d.GraphModel.from_block(r["text"]).defs())
        elif kind == "tree":
            assets.set_widget_tree(bp, r["text"], r["root"])
        elif kind == "tree_replace":
            assets.tree_replace(bp, r["root"], r["replace"])
        elif kind == "python":
            exec(compile(r["code"], "<bp_apply raw python>", "exec"), {"unreal": unreal, "bp": bp, "asset_path": path, "ctx": ctx, "core": core})
        LIB.compile_blueprint(bp)

    unreal.log("%s %s" % (FINAL_MARK, path))   # compile errors before this line are states the edit passed through
    LIB.compile_blueprint(bp)
    final = "final_%d.t3d" % index
    text = core.export_text(bp, final)
    report["final"] = final
    report["nodes"] = len(re.findall(r"^\s*Begin Object Class=\S*K2Node_\w+ Name=", text, re.M))
    report["links"] = text.count("LinkedTo=(")
    report["object_path"] = pth(bp)
    return report, bp
