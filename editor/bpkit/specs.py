"""Node kinds and batch validation for bp_apply. Pure text, no `unreal`: validate() runs on the host
before any hidden process is launched.

A node kind function takes (node spec, env) and returns (class path, body lines); the engine then adds the pins
(through a template) and the editor-side code retargets them. env supplies:
    env.class_text(path)      T3D reference text of a class, e.g. /Script/CoreUObject.Class'/Script/Engine.Actor'
    env.self_class_text()     the same for the asset's own generated class
    env.timeline_guid(name)   guid of a timeline template on the asset
"""
import re

from . import t3d
from .t3d import SpecError

CALL = "/Script/BlueprintGraph.K2Node_CallFunction"
BP = "/Script/BlueprintGraph."
RESERVED = ("entry", "result", "in", "out")


def _req(node, *keys):
    for k in keys:
        if k not in node or node[k] in (None, ""):
            raise SpecError("node %r (k=%s) needs %r" % (node.get("id"), node.get("k"), k))


def _call(node, env):
    _req(node, "function")
    parent = env.class_text(node["class"]) if node.get("class") else env.self_class_text()
    return CALL, ['FunctionReference=(MemberParent="%s",MemberName="%s")' % (parent, node["function"])]


def _var(kind):
    def build(node, env):
        _req(node, "name")
        if node.get("class"):
            ref = 'VariableReference=(MemberParent="%s",MemberName="%s")' % (env.class_text(node["class"]), node["name"])
        else:
            ref = 'VariableReference=(MemberName="%s",bSelfContext=True)' % node["name"]
        return BP + "K2Node_Variable" + kind, [ref]
    return build


def _event(node, env):
    _req(node, "name")
    return BP + "K2Node_CustomEvent", ['CustomFunctionName="%s"' % node["name"]] + [
        t3d.user_pin(p["name"], p["type"], "out") for p in node.get("params") or []]


def _override(node, env):
    _req(node, "function", "class")
    return BP + "K2Node_Event", ['EventReference=(MemberParent="%s",MemberName="%s")' % (env.class_text(node["class"]), node["function"]),
                                 "bOverrideFunction=True"]


def _plain(cls):
    return lambda node, env: (cls, [])


def _cast(node, env):
    _req(node, "target")
    return BP + "K2Node_DynamicCast", ['TargetType="%s"' % env.class_text(node["target"])]


def _delegate(cls):
    def build(node, env):
        _req(node, "name")
        return BP + cls, ['DelegateReference=(MemberName="%s",bSelfContext=True)' % node["name"]]
    return build


def _timeline(node, env):
    _req(node, "name")
    return BP + "K2Node_Timeline", ['TimelineName="%s"' % node["name"], "TimelineGuid=%s" % env.timeline_guid(node["name"])]


def _message(node, env):
    _req(node, "function", "interface")
    return BP + "K2Node_Message", ['FunctionReference=(MemberParent="%s",MemberName="%s")' % (env.class_text(node["interface"]), node["function"])]


def _manual(node, env):
    return None, None


def _raw(node, env):
    _req(node, "class", "props")
    return node["class"], list(node["props"])


NODE_KINDS = {
    "call": _call,
    "get": _var("Get"),
    "set": _var("Set"),
    "event": _event,
    "override": _override,
    "branch": _plain(BP + "K2Node_IfThenElse"),
    "sequence": _plain(BP + "K2Node_ExecutionSequence"),
    "cast": _cast,
    "spawn": _plain(BP + "K2Node_SpawnActorFromClass"),
    "create_widget": _plain("/Script/UMGEditor.K2Node_CreateWidget"),
    "call_dispatcher": _delegate("K2Node_CallDelegate"),
    "bind_dispatcher": _delegate("K2Node_AddDelegate"),
    "timeline": _timeline,
    "message": _message,
    "self": _manual,
    "macro": _manual,
    "node": _raw,
}
MANUAL_KINDS = ("self", "macro")

REQUIRED = {
    "call": ("function",), "get": ("name",), "set": ("name",), "event": ("name",), "override": ("function", "class"),
    "cast": ("target",), "call_dispatcher": ("name", "params"), "bind_dispatcher": ("name", "params"),
    "timeline": ("name",), "message": ("function", "interface"), "macro": ("name", "inputs", "outputs"),
    "node": ("class", "props"),
}
CREATE_CLASSES = ("Blueprint", "WidgetBlueprint", "BlueprintInterface")
RAW_KINDS = ("graph", "tree", "tree_replace", "python")
# bp_apply features reported by the backend_info tool
FEATURES = ("cast_object_pin", "map_types", "pin_types", "self_event_calls")
_FORBIDDEN_CODE = tuple("delete_" + n for n in ("asset", "directory", "loaded_asset")) + ("reparent_" + "blueprint", "rename_asset")


def _check_type(problems, where, t):
    try:
        t3d.parse_type(t)
    except SpecError as e:
        problems.append("%s: %s" % (where, e))


def _check_params(problems, where, items):
    if not isinstance(items, list):
        problems.append("%s must be a list of {name,type}" % where)
        return
    for i, p in enumerate(items):
        if not isinstance(p, dict) or not p.get("name") or "type" not in p:
            problems.append("%s[%d] needs name and type" % (where, i))
        else:
            _check_type(problems, "%s[%d] (%s)" % (where, i, p["name"]), p["type"])


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_find_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_key(v, key) for v in obj)
    return False


def _split_ref(ref):
    if not isinstance(ref, str) or "." not in ref:
        return None
    node, pin = ref.split(".", 1)
    return node, pin


def _check_graph_nodes(problems, where, nodes, wires, reserved, existing_ok):
    declared = set()
    for i, n in enumerate(nodes or []):
        w = "%s.nodes[%d]" % (where, i)
        if not isinstance(n, dict):
            problems.append(w + " must be an object")
            continue
        nid = n.get("id")
        if not nid or not isinstance(nid, str):
            problems.append(w + " needs an id")
            continue
        if "." in nid:
            problems.append("%s: node id %r must not contain '.'" % (w, nid))
        if nid in declared or nid in reserved:
            problems.append("%s: duplicate or reserved node id %r" % (w, nid))
        declared.add(nid)
        k = n.get("k")
        if k not in NODE_KINDS:
            problems.append("%s: unknown kind %r (known: %s)" % (w, k, ", ".join(sorted(NODE_KINDS))))
            continue
        for key in REQUIRED.get(k, ()):
            if key not in n or n[key] in (None, ""):
                problems.append("%s (k=%s) needs %r" % (w, k, key))
        for key in ("params", "inputs", "outputs"):
            if key in n:
                _check_params(problems, "%s.%s" % (w, key), n[key])
        if "pin_types" in n:
            pt = n["pin_types"]
            if k in MANUAL_KINDS:
                problems.append("%s: pin_types is not offered for k=%s" % (w, k))
            elif not isinstance(pt, dict) or not pt:
                problems.append("%s.pin_types must be an object {pin name: type}" % w)
            else:
                for pin, ts in pt.items():
                    pw = "%s.pin_types[%r]" % (w, pin)
                    if not isinstance(pin, str) or not pin:
                        problems.append(w + ".pin_types keys must be pin names")
                    elif ts == "exec":
                        problems.append(pw + ": exec is not a data type")
                    else:
                        _check_type(problems, pw, ts)
        if k in ("get", "set") and n.get("type"):
            _check_type(problems, w + ".type", n["type"])
    for i, wr in enumerate(wires or []):
        if not (isinstance(wr, list) and len(wr) == 2):
            problems.append("%s.wires[%d] must be [\"id.pin\", \"id.pin\"]" % (where, i))
            continue
        for ref in wr:
            sp = _split_ref(ref)
            if sp is None:
                problems.append("%s.wires[%d]: %r is not \"id.pin\"" % (where, i, ref))
            elif sp[0] not in declared and sp[0] not in reserved and not (existing_ok and re.match(r"^[A-Za-z0-9_]+$", sp[0])):
                problems.append("%s.wires[%d]: unknown node id %r" % (where, i, sp[0]))
    return declared


def validate(batch, mod):
    """Problems found in a bp_apply batch, as strings. Empty means it may run."""
    problems = []
    if not isinstance(batch, dict) or not isinstance(batch.get("assets"), list) or not batch["assets"]:
        return ["batch must be {\"assets\": [<asset spec>, ...]} with at least one asset"]
    prefix = "/%s/" % mod
    for ai, a in enumerate(batch["assets"]):
        w = "assets[%d]" % ai
        if not isinstance(a, dict):
            problems.append(w + " must be an object")
            continue
        path = a.get("asset")
        if not isinstance(path, str) or not path.startswith("/"):
            problems.append(w + ": 'asset' must be a /<Mod>/... path")
            continue
        w = "%s (%s)" % (w, path)
        if path.startswith("/Game/") or path == "/Game":
            problems.append(w + ": /Game is base content and is never edited")
        elif not path.startswith(prefix):
            problems.append(w + ": asset path must start with %s (other roots are refused)" % prefix)
        elif ".." in path or "." in path.rsplit("/", 1)[-1] or path.endswith("/"):
            problems.append(w + ": use a package path like %sFolder/BP_X (no dot, no ..)" % prefix)
        if _find_key(a, "reparent"):
            problems.append(w + ": reparenting is not offered")
        create = a.get("create")
        if create is not None:
            if not isinstance(create, dict) or create.get("class") not in CREATE_CLASSES:
                problems.append("%s.create.class must be one of %s" % (w, "|".join(CREATE_CLASSES)))
        for i, v in enumerate(a.get("variables") or []):
            if not isinstance(v, dict) or not v.get("name") or "type" not in v:
                problems.append("%s.variables[%d] needs name and type" % (w, i))
            else:
                _check_type(problems, "%s.variables[%d] (%s)" % (w, i, v["name"]), v["type"])
        for i, c in enumerate(a.get("components") or []):
            if not isinstance(c, dict) or not c.get("name"):
                problems.append("%s.components[%d] needs a name" % (w, i))
        for i, d in enumerate(a.get("dispatchers") or []):
            if not isinstance(d, dict) or not d.get("name"):
                problems.append("%s.dispatchers[%d] needs a name" % (w, i))
            else:
                _check_params(problems, "%s.dispatchers[%d].params" % (w, i), d.get("params") or [])
        for i, t in enumerate(a.get("timelines") or []):
            if not isinstance(t, dict) or not t.get("name") or not t.get("tracks"):
                problems.append("%s.timelines[%d] needs a name and tracks" % (w, i))
        if a.get("widget_tree") is not None:
            root = a["widget_tree"].get("root") if isinstance(a["widget_tree"], dict) else None
            if not isinstance(root, dict):
                problems.append(w + ".widget_tree needs a root widget")
            else:
                try:
                    t3d.widget_tree_text(root)
                except SpecError as e:
                    problems.append("%s.widget_tree: %s" % (w, e))
        for i, an in enumerate(a.get("animations") or []):
            if not isinstance(an, dict) or not an.get("name") or not an.get("tracks"):
                problems.append("%s.animations[%d] needs a name and tracks" % (w, i))
        for i, m in enumerate(a.get("macros") or []):
            mw = "%s.macros[%d]" % (w, i)
            if not isinstance(m, dict) or not m.get("name"):
                problems.append(mw + " needs a name")
                continue
            _check_params(problems, mw + ".inputs", m.get("inputs") or [])
            _check_params(problems, mw + ".outputs", m.get("outputs") or [])
            _check_graph_nodes(problems, mw, m.get("nodes"), m.get("wires"), ("in", "out"), False)
        for i, f in enumerate(a.get("functions") or []):
            fw = "%s.functions[%d]" % (w, i)
            if not isinstance(f, dict) or not f.get("name"):
                problems.append(fw + " needs a name")
                continue
            if f.get("pure"):
                problems.append(fw + ": pure functions are not supported (the engine rejects the generated pure function; make it an ordinary function)")
            _check_params(problems, fw + ".inputs", f.get("inputs") or [])
            _check_params(problems, fw + ".outputs", f.get("outputs") or [])
            _check_graph_nodes(problems, fw, f.get("nodes"), f.get("wires"), ("entry", "result"), False)
        for i, it in enumerate(a.get("interfaces") or []):
            iw = "%s.interfaces[%d]" % (w, i)
            if not isinstance(it, dict) or not it.get("class"):
                problems.append(iw + " needs the interface generated class path in 'class'")
                continue
            for j, f in enumerate(it.get("functions") or []):
                fw = "%s.functions[%d]" % (iw, j)
                if not isinstance(f, dict) or not f.get("name"):
                    problems.append(fw + " needs a name")
                    continue
                _check_params(problems, fw + ".inputs", f.get("inputs") or [])
                _check_params(problems, fw + ".outputs", f.get("outputs") or [])
                _check_graph_nodes(problems, fw, f.get("nodes"), f.get("wires"), ("entry", "result"), False)
        for i, g in enumerate(a.get("graphs") or []):
            gw = "%s.graphs[%d]" % (w, i)
            if not isinstance(g, dict) or not g.get("graph"):
                problems.append(gw + " needs 'graph' (the graph name)")
                continue
            declared = _check_graph_nodes(problems, gw, g.get("add"), g.get("wires"), RESERVED, True)
            for key in ("remove",):
                if key in g and not isinstance(g[key], list):
                    problems.append("%s.%s must be a list" % (gw, key))
            for ref in list((g.get("set") or {}).keys()) + list((g.get("objects") or {}).keys()):
                if _split_ref(ref) is None:
                    problems.append("%s: %r is not \"id.pin\"" % (gw, ref))
            for pair in g.get("unwire") or []:
                if not (isinstance(pair, list) and len(pair) == 2 and all(_split_ref(x) for x in pair)):
                    problems.append("%s.unwire entries must be [\"id.pin\", \"id.pin\"]" % gw)
        for i, r in enumerate(a.get("raw") or []):
            rw = "%s.raw[%d]" % (w, i)
            if not isinstance(r, dict) or r.get("kind") not in RAW_KINDS:
                problems.append("%s.kind must be one of %s" % (rw, "|".join(RAW_KINDS)))
                continue
            if r["kind"] == "python":
                code = r.get("code")
                if not isinstance(code, str) or not code.strip():
                    problems.append(rw + " needs code")
                else:
                    for bad in _FORBIDDEN_CODE:
                        if bad in code:
                            problems.append("%s: code may not use %s" % (rw, bad))
            elif r["kind"] in ("graph", "tree") and not r.get("text"):
                problems.append(rw + " needs text")
            elif r["kind"] == "tree_replace" and not isinstance(r.get("replace"), list):
                problems.append(rw + " needs replace: [[old, new], ...]")
    return problems
