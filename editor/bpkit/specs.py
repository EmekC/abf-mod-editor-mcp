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
    props = ['CustomFunctionName="%s"' % node["name"]] + [
        t3d.user_pin(p["name"], p["type"], "out") for p in node.get("params") or []]
    if node.get("meta"):
        meta_prop = t3d.function_meta_prop(node["meta"], node["name"])
        if meta_prop:
            props.append(meta_prop)
    return BP + "K2Node_CustomEvent", props


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
CREATE_CLASSES = ("Blueprint", "WidgetBlueprint", "BlueprintInterface", "UserDefinedStruct")
RAW_KINDS = ("graph", "tree", "tree_replace", "python")
# bp_apply features reported by the backend_info tool
FEATURES = ("cast_object_pin", "class_manager_row", "class_manager_row_tag", "event_meta", "function_graphs", "map_types", "pin_types", "self_event_calls",
            "set_types", "struct_fields", "variable_defaults")
EVENT_META_KEYS = ("tooltip", "category", "keywords")
EVENT_META_MAX = 2000
# keys that make no sense on a UserDefinedStruct asset
_STRUCT_FORBIDDEN_KEYS = ("variables", "remove_variables", "variable_defaults", "components", "dispatchers", "timelines", "widget_tree", "animations",
                          "macros", "functions", "remove_functions", "interfaces", "graphs", "raw", "class_manager_row")
_ROW_ASSET_RX = re.compile(r"^/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)+$")
_ROW_CLASS_RX = re.compile(r"^/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)+\.[A-Za-z0-9_]+_C$")
_ROW_KEYS = ("asset_path", "class_path", "gameplay_tag")
# same rule as tagsini.TAG_RX (the host module cannot be imported editor side); a unit test keeps them equal
TAG_RX = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"
TAG_MAX = 200
_TAG_RE = re.compile(TAG_RX)
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


def _check_event_meta(problems, where, meta):
    if not isinstance(meta, dict):
        problems.append("%s must be an object {tooltip, category, keywords}" % where)
        return
    for key, value in meta.items():
        if key not in EVENT_META_KEYS:
            problems.append("%s: unknown key %r (known: %s)" % (where, key, ", ".join(EVENT_META_KEYS)))
        elif not isinstance(value, str):
            problems.append("%s.%s must be a string" % (where, key))
        elif len(value) > EVENT_META_MAX:
            problems.append("%s.%s is longer than %d characters" % (where, key, EVENT_META_MAX))


def check_struct_fields(w, a):
    """Problems of a UserDefinedStruct asset spec: create.class UserDefinedStruct with struct_fields (and nothing else)."""
    problems = []
    fields = a.get("struct_fields")
    if not isinstance(fields, list) or not fields:
        return [w + ": a UserDefinedStruct needs struct_fields: [{name, friendly, guid, type}, ...] (at least one)"]
    for key in _STRUCT_FORBIDDEN_KEYS:
        if a.get(key):
            problems.append("%s: a UserDefinedStruct cannot have %r" % (w, key))
    seen = set()
    for i, f in enumerate(fields):
        fw = "%s.struct_fields[%d]" % (w, i)
        if not isinstance(f, dict) or not all(isinstance(f.get(k), str) and f.get(k) for k in ("name", "friendly", "guid", "type")):
            problems.append(fw + " needs name, friendly, guid and type (strings)")
            continue
        m = t3d.STRUCT_MEMBER_RX.match(f["name"])
        if not m:
            problems.append("%s: name %r must be <Field>_<index>_<32 hex GUID> (upper case)" % (fw, f["name"]))
        else:
            if m.group(3) != f["guid"]:
                problems.append("%s: guid %r is not the 32-hex suffix of the name %r" % (fw, f["guid"], f["name"]))
            if m.group(1) != f["friendly"]:
                problems.append("%s: friendly %r is not the field part of the name %r" % (fw, f["friendly"], f["name"]))
        if f["name"] in seen:
            problems.append("%s: duplicate name %r" % (fw, f["name"]))
        seen.add(f["name"])
        try:
            t3d.struct_var_line(f)
        except SpecError as e:
            problems.append("%s (%s): %s" % (fw, f["name"], e))
    return problems


def check_class_manager_row(w, row, mod):
    """Problems of an asset spec's class_manager_row: {asset_path, class_path, gameplay_tag?}, both paths inside the mod,
    never /Game; the optional gameplay_tag is a dotted tag name written to the row's gameplay tag."""
    if not isinstance(row, dict):
        return [w + ".class_manager_row must be an object {asset_path, class_path}"]
    problems = []
    for key in row:
        if key not in _ROW_KEYS:
            problems.append("%s.class_manager_row: unknown key %r (known: %s)" % (w, key, ", ".join(_ROW_KEYS)))
    prefix = "/%s/" % mod
    for key, rx, shape in (("asset_path", _ROW_ASSET_RX, "/<Mod>/Folder/RowName"), ("class_path", _ROW_CLASS_RX, "/<Mod>/Folder/BP_X.BP_X_C")):
        value = row.get(key)
        kw = "%s.class_manager_row.%s" % (w, key)
        if not isinstance(value, str) or not value:
            problems.append("%s is required (a string like %s)" % (kw, shape))
        elif value.startswith("/Game/") or value == "/Game":
            problems.append(kw + ": /Game is base content and is never edited")
        elif not rx.match(value):
            problems.append("%s: %r is not a path like %s" % (kw, value, shape))
        elif not value.startswith(prefix):
            problems.append("%s: must start with %s (other roots are refused)" % (kw, prefix))
    if "gameplay_tag" in row:
        tag = row["gameplay_tag"]
        kw = w + ".class_manager_row.gameplay_tag"
        if not isinstance(tag, str) or not tag:
            problems.append(kw + " must be a non-empty string like ClassManager.EntityDirector.Name")
        elif len(tag) > TAG_MAX:
            problems.append("%s: longer than %d characters" % (kw, TAG_MAX))
        elif "/" in tag or not _TAG_RE.match(tag):
            problems.append("%s: %r may only hold letters, digits, '_' and '-' in dot separated segments" % (kw, tag))
    return problems


ASSET_OPS = ("rename", "delete", "reparent", "recreate")
ASSET_OPS_MAX = 20
_OP_KEYS = {"rename": ("from", "to"), "delete": ("asset",), "reparent": ("asset", "parent"), "recreate": ("asset",)}
_ASSET_SEGMENT_RX = re.compile(r"^[A-Za-z0-9_]+$")
_CLASS_NAME_RX = re.compile(r"^[A-Za-z0-9_]+$")
_PROTECTED_TOP = ("AbioticModConfig", "UUGC_Metadata")
_TOOLING_TOP = ("_McpTest", "_McpTmp")


def _check_op_asset(where, value, mod):
    """None, or the problem with one asset package path of an asset op."""
    if not isinstance(value, str) or not value:
        return "%s is required (a package path like /%s/Folder/BP_X)" % (where, mod)
    if value == "/Game" or value.startswith("/Game/"):
        return "%s: /Game is base content and is never edited" % where
    prefix = "/%s/" % mod
    if not value.startswith(prefix):
        return "%s: %r must start with %s (other roots are refused)" % (where, value, prefix)
    segments = value[len(prefix):].split("/")
    if not all(_ASSET_SEGMENT_RX.match(s) for s in segments):
        return "%s: %r is not a plain package path (letters, digits and '_' in /-separated segments)" % (where, value)
    if any(s in _TOOLING_TOP for s in segments):
        return "%s: %r is tooling scratch, never an asset op target" % (where, value)
    if segments[0] in _PROTECTED_TOP:
        return "%s: %r is a mod configuration asset and is never renamed, deleted or reparented" % (where, value)
    return None


def _check_op_class(where, value, mod):
    """None, or the problem with a parent class path of a reparent op: /Script/<Module>.<Class>, or <Class> in /<Mod>/ or /Game/."""
    if not isinstance(value, str) or not value:
        return "%s is required (a class path like /Script/Engine.Actor)" % where
    if "." not in value:
        return "%s: %r is not a class path like /Script/Engine.Actor or /%s/Folder/BP_X.BP_X_C" % (where, value, mod)
    pkg, name = value.rsplit(".", 1)
    if not _CLASS_NAME_RX.match(name):
        return "%s: %r has a bad class name" % (where, value)
    parts = pkg.split("/")
    if len(parts) < 3 or parts[0] != "" or not all(_ASSET_SEGMENT_RX.match(s) for s in parts[1:]):
        return "%s: %r is not a plain class path" % (where, value)
    root = parts[1]
    if root == "Script":
        if len(parts) != 3:
            return "%s: %r is not a /Script/<Module>.<Class> path" % (where, value)
    elif root not in ("Game", mod):
        return "%s: %r must be a /Script/..., /Game/... or /%s/... class" % (where, value, mod)
    elif any(s in _TOOLING_TOP or s in _PROTECTED_TOP for s in parts[2:]):
        return "%s: %r is not a usable parent class" % (where, value)
    return None


def check_asset_ops(ops, mod):
    """Problems of a bp_asset_ops request: [{op:"rename",from,to} | {op:"delete",asset} | {op:"reparent",asset,parent} |
    {op:"recreate",asset}], at most ASSET_OPS_MAX, every path inside the mod and never /Game. Pure text: whether the
    assets exist is checked by the caller (host: staged files, editor side: asset registry)."""
    if not isinstance(ops, list) or not ops:
        return ["ops must be a non-empty list of {op, ...} objects"]
    problems = []
    if len(ops) > ASSET_OPS_MAX:
        problems.append("at most %d ops per call (got %d)" % (ASSET_OPS_MAX, len(ops)))
    for i, o in enumerate(ops[:ASSET_OPS_MAX * 2]):
        w = "ops[%d]" % i
        if not isinstance(o, dict):
            problems.append(w + " must be an object")
            continue
        kind = o.get("op")
        if kind not in ASSET_OPS:
            problems.append("%s.op must be one of %s (got %r)" % (w, "|".join(ASSET_OPS), kind))
            continue
        w = "%s (%s)" % (w, kind)
        for key in o:
            if key != "op" and key not in _OP_KEYS[kind]:
                problems.append("%s: unknown key %r (known: %s)" % (w, key, ", ".join(_OP_KEYS[kind])))
        for key in _OP_KEYS[kind]:
            if key == "parent":
                bad = _check_op_class("%s.parent" % w, o.get(key), mod)
            else:
                bad = _check_op_asset("%s.%s" % (w, key), o.get(key), mod)
            if bad:
                problems.append(bad)
        if kind == "rename" and isinstance(o.get("from"), str) and o.get("from") == o.get("to"):
            problems.append("%s: from and to are the same path" % w)
    return problems


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
        if "meta" in n:
            if k != "event":
                problems.append("%s: meta is only offered for k=event" % w)
            else:
                _check_event_meta(problems, w + ".meta", n["meta"])
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
        is_struct = isinstance(create, dict) and create.get("class") == "UserDefinedStruct"
        if is_struct:
            problems.extend(check_struct_fields(w, a))
            continue
        if "struct_fields" in a:
            problems.append(w + ": struct_fields is only valid with create.class UserDefinedStruct")
        if "class_manager_row" in a:
            problems.extend(check_class_manager_row(w, a["class_manager_row"], mod))
        for i, v in enumerate(a.get("variables") or []):
            if not isinstance(v, dict) or not v.get("name") or "type" not in v:
                problems.append("%s.variables[%d] needs name and type" % (w, i))
            else:
                _check_type(problems, "%s.variables[%d] (%s)" % (w, i, v["name"]), v["type"])
                if v.get("default") is not None and not isinstance(v["default"], (str, int, float, bool)):
                    # a number or a bool is still accepted (turned into text, bool as true/false) for older callers
                    problems.append("%s.variables[%d] (%s): default must be a string (Unreal's text form)" % (w, i, v["name"]))
        vd = a.get("variable_defaults")
        if vd is not None:
            if not isinstance(vd, dict):
                problems.append("%s.variable_defaults must be an object {name: default text or null}" % w)
            else:
                for k, val in vd.items():
                    if val is not None and not isinstance(val, str):
                        problems.append("%s.variable_defaults[%s] must be a string or null" % (w, k))
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
        seen_fns = set()
        for i, f in enumerate(a.get("functions") or []):
            fw = "%s.functions[%d]" % (w, i)
            if not isinstance(f, dict) or not f.get("name"):
                problems.append(fw + " needs a name")
                continue
            if f["name"] in seen_fns:
                problems.append("%s: function %s is listed twice" % (fw, f["name"]))
            seen_fns.add(f["name"])
            if "access" in f and f["access"] not in ("public", "protected", "private"):
                problems.append("%s.access must be public, protected or private" % fw)
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
                if "access" in f:
                    problems.append(fw + ": access is not allowed on an interface function")
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
