"""Editor-side graph work: pin templates from a throwaway scratch Blueprint, whole-graph writes, graph edits.

Every graph edit is: export -> GraphModel (text) -> change -> one write_graph that swaps the node array.
"""
import contextlib
import ctypes
import re
import struct

import unreal

from . import core, layout, specs, t3d
from .core import CFG, LIB, must, pth, addr, rd
from .t3d import GraphModel, GraphNode, SpecError

_SCRATCH = [0]
_GHOST_DROP = ("EnabledState=", "bCommentBubblePinned=", "bCommentBubbleVisible=", "NodeComment=")


def write_graph(graph, defs):
    """Replace a graph's whole node set with `defs` [(class, name, lines)] by importing them into a carrier graph and
    swapping the Nodes arrays. The old nodes move to /Engine/Transient; the new ones are renamed into the graph."""
    if not defs:
        raise SpecError("a graph cannot be written with no nodes")
    txt = '   Begin Object Class=/Script/Engine.EdGraph Name="@@NAME@@"\n' + t3d.graph_text(defs) + "\n   End Object"
    ig = core.carry(txt, "BkGraph", unreal.EdGraph)
    new = core.nodes_of(ig)
    if len(new) != len(defs):
        raise RuntimeError("graph import produced %d of %d nodes" % (len(new), len(defs)))
    found = core.nodes_array(ig, {addr(x) for x in new}, len(new))
    if found is None:
        raise RuntimeError("Nodes array not found in the imported graph")
    off = found[0]
    all_old = core.nodes_of(graph)
    old = []
    if all_old:
        real = core.nodes_array(graph, {addr(x) for x in all_old})
        if real is None:
            raise RuntimeError("Nodes array not found in %s (%d nodes owned)" % (pth(graph), len(all_old)))
        if real[0] != off:
            raise RuntimeError("Nodes offset mismatch: graph %s, carrier %s" % (real[0], off))
        inarr = set(real[1])
        old = [n for n in all_old if addr(n) in inarr]
    core.swap(addr(ig) + off, addr(graph) + off, 16)
    ctypes.memmove(addr(ig) + off + 8, struct.pack("<i", 0), 4)
    tr = must(unreal.find_object(None, "/Engine/Transient"), "transient package")
    for n in old:
        n.rename("BkOldN_" + t3d.gid()[:10], tr)
    base = pth(graph) + "."
    for n in new:
        if unreal.find_object(None, base + n.get_name()) is not None:
            raise RuntimeError("node name taken: " + n.get_name())
    for n in new:
        n.rename(None, graph)
    return len(old), len(new)


def _rebuild_scratch_pins(bp):
    """Rebuild the pins of nodes in the throwaway scratch Blueprint (plain compile does not). Scratch only."""
    if "BP_BkScratch" not in pth(bp):
        raise RuntimeError("refusing to reparent a Blueprint that is not a scratch one: " + pth(bp))
    LIB.reparent_blueprint(bp, unreal.Pawn)
    LIB.reparent_blueprint(bp, unreal.Actor)


def make_templates(items, scratch_vars=(), setups=(), self_events=()):
    """items: {key: (class_path, body_lines)}. Returns {key: [lines]} with every pin the engine would make.
    self_events: [(name, params)] custom events the spec adds; they exist in the scratch graph so a self call to one
    gets its pins before the real asset has the event. The scratch Blueprint (parent Actor) lives under CFG.dest and is never saved."""
    from . import assets
    _SCRATCH[0] += 1
    fac = unreal.BlueprintFactory()
    fac.set_editor_property("parent_class", unreal.Actor)
    z = must(core.tools().create_asset("BP_BkScratch%d" % _SCRATCH[0], CFG.dest, unreal.Blueprint, fac), "scratch Blueprint")
    if scratch_vars:
        assets.set_vars(z, list(scratch_vars))
        LIB.compile_blueprint(z)
    for s in setups:
        s(z)
        LIB.compile_blueprint(z)
    g = must(LIB.find_event_graph(z), "scratch event graph")
    defs = [(c, "T_" + k, list(b) + ["NodeGuid=" + t3d.gid()]) for k, (c, b) in items.items()]
    for i, (name, params) in enumerate(self_events):
        c, b = specs._event({"name": name, "params": params or []}, None)
        defs.append((c, "E_%d" % i, list(b) + ["NodeGuid=" + t3d.gid()]))
    write_graph(g, defs)
    _rebuild_scratch_pins(z)
    t = core.export_text(z, "bk_tmpl.t3d").split("\n")
    out = {}
    for k in items:
        key = 'Begin Object Name="T_%s"' % k
        idx = [n for n, l in enumerate(t) if key in l]
        if not idx:
            raise RuntimeError("template for %s not found in the scratch export" % k)
        i = idx[-1]
        ind = len(t[i]) - len(t[i].lstrip())
        ls = []
        for l in t[i + 1:]:
            if l.startswith(" " * ind + "End Object"):
                break
            s = l.strip()
            if s.startswith(("NodePosX", "NodePosY", "NodeGuid")):
                continue
            ls.append(s.rstrip("\r"))
        out[k] = ls
    return out


def load_graph_model(bp, graph_name):
    LIB.compile_blueprint(bp)
    block = t3d.graph_block(core.export_text(bp, "bk_graph.t3d"), graph_name)
    if not block.strip():
        raise RuntimeError("graph %s not found in the export of %s" % (graph_name, pth(bp)))
    return GraphModel.from_block(block)


def find_graph(bp, name):
    if name == "EventGraph":
        return must(LIB.find_event_graph(bp), "EventGraph")
    return must(LIB.find_graph(bp, name), "graph " + name)


def _reserved(model):
    out = {}
    for n in model.nodes.values():
        c = n.cls.split(".")[-1]
        if c == "K2Node_FunctionEntry":
            out.setdefault("entry", n.name)
        elif c == "K2Node_FunctionResult":
            out.setdefault("result", n.name)
        elif c == "K2Node_Tunnel":
            if "bCanHaveOutputs=True" in n.head:
                out.setdefault("in", n.name)
            elif "bCanHaveInputs=True" in n.head:
                out.setdefault("out", n.name)
    return out


def reuse_ghost(model, spec):
    """An `override` node whose EventReference matches an existing K2Node_Event reuses that (usually disabled) node."""
    want = 'MemberName="%s"' % spec["function"]
    for n in model.nodes.values():
        if n.cls.endswith("K2Node_Event"):
            if any(l.startswith("EventReference=") and want in l for l in n.head):
                n.head = [h for h in n.head if not h.startswith(_GHOST_DROP)]
                return n
    return None


def _size(node):
    ins = sum(1 for p in node.pins if p.direction == "in")
    return layout.node_size(ins, len(node.pins) - ins)


def _build_node(spec, model, templates, ctx):
    k = spec["k"]
    env = ctx["env"]
    if k == "self":
        return t3d.self_node(model.unique_name(t3d.SELF_CLASS), (0, 0))
    if k == "macro":
        info = ctx["macros"].get(spec["name"])
        if info is None:
            raise SpecError("unknown macro %s (define it in 'macros' first)" % spec["name"])
        guid, ins, outs = info
        return t3d.macro_instance_node(model.unique_name(t3d.MACRO_CLASS), ctx["bp_path"], guid,
                                       spec.get("inputs") or ins, spec.get("outputs") or outs, (0, 0))
    cls, _body = specs.NODE_KINDS[k](spec, env)
    key = (ctx["scope"], spec["id"])
    if key not in templates:
        raise SpecError("no template for node %s in %s" % (spec["id"], ctx["scope"]))
    node = GraphNode.from_template(cls, model.unique_name(cls), templates[key], (0, 0))
    if k == "call" and not spec.get("class"):
        # a call to the asset's own function: self context, not an external class reference
        # (a template for an event added by the same spec already is bSelfContext and has no MemberParent: unchanged)
        node.head = [re.sub(r'FunctionReference=\(MemberParent="[^"]*",MemberName="([^"]+)"', r'FunctionReference=(MemberName="\1",bSelfContext=True', h)
                     for h in node.head]
    for pin, ts in (spec.get("pin_types") or {}).items():
        node.resolve_wildcard(pin, t3d.parse_type(ts, ctx["env"].class_text))
    if k == "std_macro" and spec.get("pin_types"):
        # the element type: the "Array Element" entry (any key order), else the first entry (set_resolved_wildcard drops its container)
        pts = spec["pin_types"]
        node.set_resolved_wildcard(t3d.parse_type(pts.get("Array Element") or next(iter(pts.values())), ctx["env"].class_text))
    node.fill_default_wildcards()
    return node


@contextlib.contextmanager
def _spec_error(spec_id):
    """Prefix a SpecError with the spec id of the node it is about, so a caller can map it back to its source."""
    try:
        yield
    except SpecError as e:
        raise SpecError("[spec:%s] %s" % (spec_id, e)) from None


def edit_graph(bp, graph, edit, templates, ctx):
    """edit: remove, unwire, add, wires, set, objects, move (in that order). Returns {id: node name}.
    ctx: env, scope, macros, bp_path. Reserved ids: entry/result (functions), in/out (macros)."""
    model = load_graph_model(bp, graph.get_name())
    reserved = _reserved(model)
    ids = {}

    def name_of(i):
        if i in ids:
            return ids[i]
        if i in reserved:
            return reserved[i]
        if i in model.nodes:
            return i
        raise SpecError("unknown node id %r in graph %s (known: %s)" % (i, graph.get_name(), ", ".join(list(ids) + list(reserved) + list(model.nodes)[:20])))

    def split(ref):
        node, pin = ref.split(".", 1)
        return name_of(node), pin

    for r in edit.get("remove") or []:
        model.remove(name_of(r))
    for a, b in edit.get("unwire") or []:
        (na, pa), (nb, pb) = split(a), split(b)
        model.unwire(na, pa, nb, pb)
    occupied = [(n.pos()[0], n.pos()[1]) + _size(n) for n in model.nodes.values()]
    fresh = []
    for spec in edit.get("add") or []:
        ghost = reuse_ghost(model, spec) if spec["k"] == "override" else None
        if ghost is not None:
            ids[spec["id"]] = ghost.name
            node = ghost
        else:
            node = _build_node(spec, model, templates, ctx)
            model.add(node)
            ids[spec["id"]] = node.name
            if spec.get("pos"):
                node.move(spec["pos"][0], spec["pos"][1])
                occupied.append((spec["pos"][0], spec["pos"][1]) + _size(node))
            else:
                fresh.append((spec["id"], node))
        for pin, val in (spec.get("set") or {}).items():
            with _spec_error(spec["id"]):
                node.set_default(pin, val)
        for pin, path in (spec.get("objects") or {}).items():
            node.set_default_object(pin, path if "'" in path else env_object_text(ctx["env"], path))
    fresh_ids = {i for i, _ in fresh}
    wires = [(a.split(".", 1)[0], b.split(".", 1)[0]) for a, b in (edit.get("wires") or [])
             if a.split(".", 1)[0] in fresh_ids and b.split(".", 1)[0] in fresh_ids]
    spots = layout.place([(i, ) + _size(n) for i, n in fresh], wires, occupied)
    for i, n in fresh:
        n.move(*spots[i])
    for a, b in edit.get("wires") or []:
        (na, pa), (nb, pb) = split(a), split(b)
        try:
            model.get(na).pin(pa, "out")
            failing = b
        except SpecError:
            failing = a
        with _spec_error(failing.split(".", 1)[0]):
            model.wire(na, pa, nb, pb)
    for ref, val in (edit.get("set") or {}).items():
        n, pin = split(ref)
        with _spec_error(ref.split(".", 1)[0]):
            model.get(n).set_default(pin, val)
    for ref, path in (edit.get("objects") or {}).items():
        n, pin = split(ref)
        model.get(n).set_default_object(pin, path if "'" in path else env_object_text(ctx["env"], path))
    for i, xy in (edit.get("move") or {}).items():
        model.get(name_of(i)).move(xy[0], xy[1])
    write_graph(graph, model.defs())
    return dict(ids)


def env_object_text(env, path):
    return env.class_text(path)
