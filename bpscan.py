"""Offline Blueprint helpers: parse T3D exports and check references against the
Blueprint filter the upload Asset Audit and the mod.io cloud cook enforce.

Filter format (cdn.abfmods.com/blueprint-filter/1.5/BlueprintFilter.txt):
    Allow|Block /Script/Module
    Allow|Block /Script/Module.Class          (or /Game/Path/Asset.Asset_C)
    Allow|Block /Script/Module.Class Member   (function or variable)
The most specific rule wins. Modules not listed at all are reported as "unlisted";
/Game assets with no rule are treated as allowed, since the filter lists only exceptions.
"""
import re
import time
import urllib.request
from pathlib import Path

FILTER_URL = "https://cdn.abfmods.com/blueprint-filter/1.5/BlueprintFilter.txt"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
HERE = Path(__file__).resolve().parent
FILTER_FILE = HERE / "data" / "BlueprintFilter.txt"


# ---- filter -----------------------------------------------------------------

def load_filter(max_age_hours=24):
    """Return (rules, meta). Downloads a fresh copy when the cached one is older than max_age_hours."""
    meta = {"source": str(FILTER_FILE)}
    stale = not FILTER_FILE.is_file() or time.time() - FILTER_FILE.stat().st_mtime > max_age_hours * 3600
    if stale:
        try:
            req = urllib.request.Request(FILTER_URL, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if data.startswith(b"# ABF-BlueprintFilter"):
                FILTER_FILE.parent.mkdir(exist_ok=True)
                FILTER_FILE.write_bytes(data)
                meta["downloaded"] = True
        except Exception as e:                       # offline: fall back to the cached copy
            meta["download_error"] = str(e)
    if not FILTER_FILE.is_file():
        raise RuntimeError("No Blueprint filter available (download failed and no cached copy).")
    rules = {}
    for line in FILTER_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# ContentVersion"):
            meta["content_version"] = line.split()[-1]
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("Allow", "Block"):
            key = parts[1] + (" " + parts[2] if len(parts) > 2 else "")
            rules[key] = parts[0]
    meta["rule_count"] = len(rules)
    return rules, meta


def resolve(rules, parent, member=None):
    """(verdict, matched_rule) for a reference to `member` on class/asset path `parent`."""
    if member and f"{parent} {member}" in rules:
        return rules[f"{parent} {member}"], f"{parent} {member}"
    if parent in rules:
        return rules[parent], parent
    module = parent.split(".")[0]
    if module in rules:
        return rules[module], module
    if parent.startswith("/Game/") or parent.startswith("/Engine/"):
        return "Allow", None                          # the filter lists only exceptions for content
    return "Unlisted", None


# ---- T3D parsing ------------------------------------------------------------
# A Blueprint export lists each object twice under its graph: first a bare declaration
# (`Begin Object Class=... Name=...` / `End Object`), then a definition (`Begin Object Name=...`)
# holding the properties. Both are merged by name here.

_BEGIN = re.compile(r'^(\s*)Begin Object(?: Class=(\S+))? Name="([^"]+)"')
_PATHREF = re.compile(r"""(?:Class|BlueprintGeneratedClass|WidgetBlueprintGeneratedClass|AnimBlueprintGeneratedClass|Function|ScriptStruct|Enum|UserDefinedEnum|UserDefinedStruct|Object|Package)'"?(/[^'"]+)'?"?""")


def _member_ref(body, kind):
    m = re.search(kind + r'=\((?:MemberParent="?([^",)]*)"?,)?(?:MemberScope="[^"]*",)?MemberName="([^"]+)"', body)
    if not m:
        return None
    parent = m.group(1) or ""
    pm = _PATHREF.search(parent) if parent else None
    return {"parent": pm.group(1) if pm else (parent or None), "name": m.group(2)}


def _parse_node_props(node, btxt):
    for kind, key in (("FunctionReference", "function"), ("EventReference", "event"), ("VariableReference", "variable"),
                      ("DelegateReference", "delegate"), ("MacroGraphReference", "macro")):
        ref = _member_ref(btxt, kind)
        if ref:
            node[key] = ref
    for key in ("CustomFunctionName", "InputKey", "StructType", "TargetType", "ProxyFactoryClass", "ProxyClass", "NodeComment"):
        mm = re.search(r"^\s*" + key + r"=(.*)$", btxt, re.M)
        if mm:
            node[key[0].lower() + key[1:]] = mm.group(1).strip().strip('"')[:160]
    px = re.search(r"^\s*NodePosX=(-?\d+)", btxt, re.M)
    py = re.search(r"^\s*NodePosY=(-?\d+)", btxt, re.M)
    node["pos"] = [int(px.group(1)) if px else 0, int(py.group(1)) if py else 0]
    guid = re.search(r"^\s*NodeGuid=([0-9A-Fa-f]{32})", btxt, re.M)
    node["guid"] = guid.group(1) if guid else None
    err = re.search(r'ErrorMsg="(.*)"', btxt)
    if err:
        node["error"] = err.group(1)[:300]
    orphans = re.findall(r'PinName="([^"]+)"[^\n]*bOrphanedPin=True', btxt)
    if orphans:
        node["orphaned_pins"] = orphans
    pins = []
    for pin in re.findall(r"^\s*CustomProperties Pin \((.*)\)\s*$", btxt, re.M):
        name = re.search(r'PinName="([^"]*)"', pin)
        cat = re.search(r'PinType\.PinCategory="([^"]*)"', pin)
        obj = re.search(r"PinType\.PinSubCategoryObject=([^,]*)", pin)
        linked = re.findall(r"LinkedTo=\(([^)]*)\)", pin)
        entry = {"name": name.group(1) if name else "?", "type": cat.group(1) if cat else "?",
                 "dir": "out" if 'Direction="EGPD_Output"' in pin else "in"}
        if obj and obj.group(1) not in ("None", ""):
            pm = _PATHREF.search(obj.group(1))
            entry["object"] = pm.group(1) if pm else obj.group(1).strip('"')
        pid = re.search(r"PinId=([0-9A-Fa-f]+)", pin)
        entry["id"] = pid.group(1) if pid else None
        entry["links"] = []
        if linked and linked[0]:
            entry["linked"] = [l.split(" ")[0] for l in linked[0].split(",") if l]
            for l in linked[0].split(","):
                parts = l.strip().split(" ")
                if len(parts) == 2:
                    entry["links"].append({"node": parts[0], "pin_id": parts[1]})
        default = re.search(r'DefaultValue="([^"]*)"', pin)
        if default and default.group(1):
            entry["default"] = default.group(1)[:80]
        pins.append(entry)
    node["pins"] = pins
    node["object_refs"] = sorted({r for r in _PATHREF.findall(btxt) if r.startswith(("/Script/", "/Game/", "/Engine/"))})


def parse_t3d(text):
    """Return {'blueprint': {...}, 'graphs': {graph: node_count}, 'nodes': [...]}."""
    lines = text.splitlines()
    objects = {}          # name -> {'class':..., 'graph':..., 'body': [lines]}
    order = []
    stack = []            # names of enclosing objects
    bp = {}
    for line in lines:
        m = _BEGIN.match(line)
        if m:
            cls, name = (m.group(2) or ""), m.group(3)
            graph = stack[-1] if stack else None
            if name not in objects:
                objects[name] = {"class": "", "graph": graph, "body": []}
                order.append(name)
            if cls:
                objects[name]["class"] = cls
            stack.append(name)
            continue
        if line.strip() == "End Object":
            if stack:
                stack.pop()
            continue
        if stack:
            objects[stack[-1]]["body"].append(line)
    nodes, graphs = [], {}
    for name in order:
        o = objects[name]
        short = o["class"].split(".")[-1]
        btxt = "\n".join(o["body"])
        if not stack and not bp and "Blueprint" in short and o["graph"] is None:
            bp = {"class": short, "name": name}
            pc = re.search(r"^\s*ParentClass=(.*)$", btxt, re.M)
            if pc:
                pm = _PATHREF.search(pc.group(1))
                bp["parent_class"] = pm.group(1) if pm else pc.group(1).strip()
            bp["variables"] = re.findall(r'NewVariables\(\d+\)=\(VarName="([^"]+)"', btxt)
            bp["interfaces"] = [_PATHREF.search(x).group(1) for x in re.findall(r"ImplementedInterfaces\(\d+\)=\((.*)\)", btxt) if _PATHREF.search(x)]
            bp["components"] = re.findall(r'Begin Object Class=\S+ Name="([^"]+)"', btxt)
        if "Node" in short:
            node = {"name": name, "class": short, "graph": o["graph"]}
            _parse_node_props(node, btxt)
            nodes.append(node)
            graphs[o["graph"] or "?"] = graphs.get(o["graph"] or "?", 0) + 1
    return {"blueprint": bp, "graphs": graphs, "nodes": nodes}


def check_nodes(nodes, rules):
    """Classify every function/variable/object reference in parsed nodes. Returns dict with blocked/unlisted lists."""
    blocked, unlisted, seen = [], [], set()
    for n in nodes:
        refs = []
        for key in ("function", "variable", "delegate"):
            r = n.get(key)
            if r and r.get("parent"):
                refs.append((r["parent"], r["name"], key))
        # Classes the node USES (cast target, spawn/construct class, async proxy), not pin types:
        # the audit checks calls and variables, and parameter/struct types never trip it.
        for key in ("targetType", "proxyClass", "proxyFactoryClass"):
            v = n.get(key)
            if v:
                pm = _PATHREF.search(v)
                refs.append((pm.group(1) if pm else v, None, key))
        for pin in n.get("pins", []):
            if pin.get("type") == "class" and pin.get("default", "").startswith("/Script/"):
                refs.append((pin["default"].split("'")[-2] if "'" in pin["default"] else pin["default"], None, "class pin " + pin["name"]))
        for parent, member, kind in refs:
            verdict, rule = resolve(rules, parent, member)
            key = (parent, member)
            if verdict == "Block":
                blocked.append({"node": n["name"], "graph": n.get("graph"), "kind": kind, "parent": parent, "member": member, "rule": rule})
            elif verdict == "Unlisted" and key not in seen:
                seen.add(key)
                unlisted.append({"parent": parent, "member": member, "first_node": n["name"]})
    return {"blocked": blocked, "unlisted": unlisted}


# ---- structure (bp_read) ----------------------------------------------------

def _build_tree(text):
    """Nested objects of a T3D export. Declaration and definition passes merge by name within the parent."""
    root = {"name": None, "cls": "", "lines": [], "children": {}}
    stack = [root]
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        m = _BEGIN.match(line)
        if m:
            cur = stack[-1]
            cls, name = (m.group(2) or ""), m.group(3)
            child = cur["children"].get(name)
            if child is None:
                child = {"name": name, "cls": "", "lines": [], "children": {}}
                cur["children"][name] = child
            if cls:
                child["cls"] = cls
            stack.append(child)
            continue
        if line.strip() == "End Object":
            if len(stack) > 1:
                stack.pop()
            continue
        stack[-1]["lines"].append(line)
    return root


def _last_name(ref):
    """"/Script/Engine.EdGraph'Foo'" or ".../Path.Asset:Graph.Foo" -> "Foo"."""
    m = re.search(r"'([^']+)'", ref)
    s = m.group(1) if m else ref.strip().strip('"')
    return re.split(r"[.:]", s)[-1]


def _array_names(lines, prop):
    out = []
    for l in lines:
        m = re.match(r"\s*" + prop + r"\(\d+\)=(.*)$", l)
        if m:
            out.append(_last_name(m.group(1)))
    return out


def _var_type(vartype):
    """Type string in the bp_apply grammar from the inside of VarType=(...) / PinType=(...)."""
    cat = re.search(r'PinCategory="([^"]*)"', vartype)
    sub = re.search(r'PinSubCategory="([^"]*)"', vartype)
    obj = re.search(r'PinSubCategoryObject="([^"]*)"', vartype)
    cont = re.search(r"ContainerType=(\w+)", vartype)
    cat = cat.group(1) if cat else "?"
    path = None
    if obj and obj.group(1) not in ("None", ""):
        pm = _PATHREF.search(obj.group(1))
        path = pm.group(1) if pm else obj.group(1)
    if cat == "real":
        t = "float" if (sub and sub.group(1) == "float") else "real"
    elif cat in ("object", "class", "softobject", "softclass", "interface", "struct") and path:
        t = cat + ":" + path
    elif cat == "byte" and path:
        t = "enum:" + path
    else:
        t = cat
    if cont and cont.group(1) != "None":
        t = cont.group(1).lower() + ":" + t
    return t, path


def _balanced(s, opener):
    """Text inside the parentheses that `opener` (ending in '(') starts, or ''."""
    i = s.find(opener)
    if i < 0:
        return ""
    depth, start = 0, i + len(opener) - 1
    for j in range(start, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return s[start + 1:j]
    return s[start + 1:]


def _user_pins(lines):
    ins, outs = [], []
    for l in lines:
        m = re.match(r'\s*CustomProperties UserDefinedPin \(PinName="([^"]*)",(.*),DesiredPinDirection=(\w+)\)\s*$', l)
        if not m:
            continue
        t, _ = _var_type(_balanced(m.group(2), "PinType=("))
        (outs if m.group(3) == "EGPD_Output" else ins).append({"name": m.group(1), "type": t})
    return ins, outs


def _graph_nodes(graph):
    names = _array_names(graph["lines"], "Nodes")
    nodes = []
    for name in names:
        ch = graph["children"].get(name)
        if ch is None:
            continue
        node = {"name": name, "class": ch["cls"].split(".")[-1], "graph": graph["name"]}
        _parse_node_props(node, "\n".join(ch["lines"]))
        node["_lines"] = ch["lines"]
        nodes.append(node)
    by_name = {n["name"]: n for n in nodes}
    for n in nodes:
        for p in n["pins"]:
            for l in p.get("links", []):
                tgt = by_name.get(l["node"])
                if tgt:
                    for tp in tgt["pins"]:
                        if tp.get("id") == l["pin_id"]:
                            l["pin"] = tp["name"]
                            break
    return nodes


def _descendants(obj):
    for c in obj["children"].values():
        yield c
        yield from _descendants(c)


def parse_structure(text):
    """Structure of a Blueprint export: variables, functions, macros, dispatchers, timelines, interfaces,
    components, graphs (nodes restricted to each graph's Nodes(i) lines) and the widget tree."""
    tree = _build_tree(text)
    bps = [c for c in tree["children"].values() if "Blueprint" in c["cls"].split(".")[-1]]
    if not bps:
        return {"blueprint": {}, "variables": [], "functions": [], "macros": [], "dispatchers": [], "timelines": [],
                "interfaces": [], "components": [], "graphs": {}, "widgets": []}
    bp = bps[0]
    lines = bp["lines"]
    info = {"class": bp["cls"].split(".")[-1], "name": bp["name"]}
    for l in lines:
        pc = re.match(r"\s*ParentClass=(.*)$", l)
        if pc:
            pm = _PATHREF.search(pc.group(1))
            info["parent_class"] = pm.group(1) if pm else pc.group(1).strip()
            break
    variables = []
    for l in lines:
        m = re.match(r'\s*NewVariables\(\d+\)=\(VarName="([^"]+)"(.*)\)\s*$', l)
        if not m:
            continue
        t, sub_obj = _var_type(_balanced(m.group(2), "VarType=("))
        d = re.search(r'DefaultValue="((?:[^"\\]|\\.)*)"', m.group(2))
        variables.append({"name": m.group(1), "type": t, "sub_object": sub_obj, "default": d.group(1) if d else None})
    graphs = {}
    for c in bp["children"].values():
        if c["cls"].split(".")[-1] == "EdGraph":
            graphs[c["name"]] = _graph_nodes(c)
    functions = []
    for name in _array_names(lines, "FunctionGraphs"):
        ins, outs = [], []
        for n in graphs.get(name, []):
            if n["class"] == "K2Node_FunctionEntry":
                ins = _user_pins(n["_lines"])[1]
            elif n["class"] == "K2Node_FunctionResult":
                outs = _user_pins(n["_lines"])[0]
        functions.append({"name": name, "inputs": ins, "outputs": outs})
    macros = _array_names(lines, "MacroGraphs")
    dispatchers = _array_names(lines, "DelegateSignatureGraphs")
    timelines = [n[:-9] if n.endswith("_Template") else n for n in _array_names(lines, "Timelines")]
    interfaces = []
    for l in lines:
        if re.match(r"\s*ImplementedInterfaces\(\d+\)=", l):
            pm = _PATHREF.search(l)
            if pm:
                interfaces.append(pm.group(1))
    components = []
    for d in _descendants(bp):
        if d["cls"].split(".")[-1] == "SCS_Node":
            body = "\n".join(d["lines"])
            vn = re.search(r'InternalVariableName="([^"]+)"', body)
            cc = re.search(r"ComponentClass=(.*)$", body, re.M)
            par = re.search(r'ParentComponentOrVariableName="([^"]+)"', body)
            pm = _PATHREF.search(cc.group(1)) if cc else None
            components.append({"name": vn.group(1) if vn else d["name"], "class": pm.group(1) if pm else None,
                               "parent": par.group(1) if par else None})
    widgets = []
    wt = bp["children"].get("WidgetTree")
    if wt:
        parent_of = {}
        for w in wt["children"].values():
            for s in w["children"].values():
                for l in s["lines"]:
                    c = re.match(r"\s*Content=(.*)$", l)
                    if c:
                        parent_of[_last_name(c.group(1))] = w["name"]
        for w in wt["children"].values():
            if w["cls"].split(".")[-1].endswith("Slot"):
                continue
            widgets.append({"name": w["name"], "class": w["cls"], "parent": parent_of.get(w["name"])})
    for g in graphs.values():
        for n in g:
            n.pop("_lines", None)
    return {"blueprint": info, "variables": variables, "functions": functions, "macros": macros,
            "dispatchers": dispatchers, "timelines": timelines, "interfaces": interfaces,
            "components": components, "graphs": graphs, "widgets": widgets}


def merge_live(st, live):
    """Merge the hidden process's live read (components, variable defaults) into parse_structure's result.
    `live` is None when the read never finished. components is None, never [], when it could not be read."""
    why = "the hidden read did not finish"
    comp = (live or {}).get("components") or {"status": "unavailable", "error": why}
    defs = (live or {}).get("defaults") or {"status": "unavailable", "error": why}
    st["components_status"] = comp["status"]
    if comp["status"] == "unavailable":
        st["components"] = None
        st["components_error"] = comp.get("error") or why
    else:
        st["components"] = comp["items"]
        if comp.get("note"):
            st["components_note"] = comp["note"]
    for v in st["variables"]:
        got = (defs.get("items") or {}).get(v["name"])
        if got and got["status"] == "ok":
            v["default"] = got["default"]
            v["default_status"] = "ok"
        elif v["default"] is not None:
            v["default_status"] = "ok"
        else:
            v["default_status"] = got["status"] if got else "unavailable"
            v["default"] = None
            err = (got or {}).get("error") or (None if got else defs.get("error") or why)
            if err:
                v["default_error"] = err
    bad = [v["default_status"] for v in st["variables"]].count("unavailable")
    if not st["variables"]:
        st["defaults_status"] = "unavailable" if defs["status"] == "unavailable" else "ok"
    elif bad == 0:
        st["defaults_status"] = "ok"
    elif bad == len(st["variables"]):
        st["defaults_status"] = "unavailable"
    else:
        st["defaults_status"] = "partial"
    return st
