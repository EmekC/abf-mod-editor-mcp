"""Pure text layer for Blueprint T3D: type grammar, pins, graph nodes and graph models, node builders,
widget trees, timelines, animations. Imports no `unreal`, so it runs on the host (unit tests) and in the editor.

Everything the tools write is the same text the engine exports, so values and defaults are raw T3D strings.
"""
import hashlib
import re
import uuid

FLAGS_FN = 201457664              # default ExtraFlags of a function entry
FLAGS_PURE = 0x10000000           # added for a pure function
FLAGS_DELEGATE = 335675392        # ExtraFlags of a dispatcher signature entry
DISPATCHER_PROPERTY_FLAGS = 17592454479877
SCHEMA_K2 = "/Script/CoreUObject.Class'/Script/BlueprintGraph.EdGraphSchema_K2'"

ENTRY_CLASS = "/Script/BlueprintGraph.K2Node_FunctionEntry"
RESULT_CLASS = "/Script/BlueprintGraph.K2Node_FunctionResult"
TUNNEL_CLASS = "/Script/BlueprintGraph.K2Node_Tunnel"
SELF_CLASS = "/Script/BlueprintGraph.K2Node_Self"
MACRO_CLASS = "/Script/BlueprintGraph.K2Node_MacroInstance"


class SpecError(ValueError):
    pass


def gid():
    return uuid.uuid4().hex.upper()


# ---- types ------------------------------------------------------------------

_SIMPLE = {
    "exec": ("exec", ""), "int": ("int", ""), "int64": ("int64", ""), "bool": ("bool", ""), "byte": ("byte", ""),
    "name": ("name", ""), "string": ("string", ""), "text": ("text", ""),
    "real": ("real", "double"), "double": ("real", "double"), "float": ("real", "float"),
}
_OBJ_KINDS = {"object": "object", "class": "class", "softobject": "softobject", "softclass": "softclass",
              "interface": "interface", "struct": "struct", "enum": "byte"}


def class_ref(path, meta="/Script/CoreUObject.Class"):
    return "%s'%s'" % (meta, path)


def _object_ref(kind, path):
    if kind == "struct":
        return class_ref(path, "/Script/CoreUObject.ScriptStruct" if path.startswith("/Script/") else "/Script/Engine.UserDefinedStruct")
    if kind == "enum":
        return class_ref(path, "/Script/CoreUObject.Enum" if path.startswith("/Script/") else "/Script/Engine.UserDefinedEnum")
    if path.startswith("/Script/"):
        return class_ref(path)
    return class_ref(path, "/Script/Engine.BlueprintGeneratedClass")


_CLASSLIKE = ("object", "class", "softobject", "softclass", "interface")


def parse_type(t, class_text=None):
    """'int', 'real', 'object:/Script/Engine.Actor', 'array:int' ... -> {"cat","sub","obj","container"}.

    class_text: optional callable(path) -> T3D class reference for non-/Script class paths
    (bp_apply passes Env.class_text)."""
    if not isinstance(t, str) or not t.strip():
        raise SpecError("type must be a non-empty string, got %r" % (t,))
    t = t.strip()
    if t.startswith("array:"):
        inner = dict(parse_type(t[6:], class_text))
        if inner["container"]:
            raise SpecError("nested containers are not supported: %s" % t)
        inner["container"] = "Array"
        return inner
    head = t.split(":", 1)[0]
    if head == "map":
        key, sep, val = t[4:].partition(":")
        if not sep or key not in _SIMPLE or key == "exec":
            raise SpecError("map types are map:<key>:<value> with a simple key type (int int64 bool byte name string text real double float): %s" % t)
        v = parse_type(val, class_text)
        if v["container"]:
            raise SpecError("nested containers are not supported: %s" % t)
        cat, sub = _SIMPLE[key]
        return {"cat": cat, "sub": sub, "obj": None, "container": "Map", "value": v}
    if head == "set":
        raise SpecError("%s types are not supported (arrays and maps only): %s" % (head, t))
    if t in _SIMPLE:
        cat, sub = _SIMPLE[t]
        return {"cat": cat, "sub": sub, "obj": None, "container": None}
    if head in _OBJ_KINDS and ":" in t:
        path = t.split(":", 1)[1].strip()
        if not path:
            raise SpecError("type %r needs a path (a struct with no type crashes the import)" % head)
        obj = _object_ref(head, path)
        if class_text is not None and head in _CLASSLIKE and not path.startswith("/Script/") and "'" not in path:
            obj = class_text(path)
        return {"cat": _OBJ_KINDS[head], "sub": "", "obj": obj, "container": None}
    if head in _OBJ_KINDS:
        raise SpecError("type %r needs a path, e.g. %s:/Script/Engine.Actor" % (head, head))
    raise SpecError("unknown type %r (exec int int64 bool byte name string text real double float "
                    "object:<path> class:<path> softobject:<path> softclass:<path> interface:<path> struct:<path> enum:<path> array:<type> map:<key>:<type>)" % t)


def _direction(d):
    if d in ("out", "output", "EGPD_Output"):
        return "EGPD_Output"
    if d in ("in", "input", "EGPD_Input"):
        return "EGPD_Input"
    raise SpecError("direction must be in or out, got %r" % (d,))


def _terminal_text(v):
    s = '(TerminalCategory="%s"' % v["cat"]
    if v["sub"]:
        s += ',TerminalSubCategory="%s"' % v["sub"]
    if v["obj"]:
        s += ',TerminalSubCategoryObject="%s"' % v["obj"]
    return s + ")"


def _pin_type_core(t):
    return ('PinType.PinCategory="%s",PinType.PinSubCategory="%s",PinType.PinSubCategoryObject=%s,'
            'PinType.PinSubCategoryMemberReference=(),PinType.PinValueType=%s,PinType.ContainerType=%s'
            % (t["cat"], t["sub"], '"%s"' % t["obj"] if t["obj"] else "None",
               _terminal_text(t["value"]) if t.get("value") else "()", t["container"] or "None"))


def _pin_type_attrs(t):
    return (_pin_type_core(t) + ',PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,'
            'PinType.bIsUObjectWrapper=False,PinType.bSerializeAsSinglePrecisionFloat=False')


def _compact_type(t):
    s = 'PinCategory="%s"' % t["cat"]
    if t["sub"]:
        s += ',PinSubCategory="%s"' % t["sub"]
    if t["obj"]:
        s += ',PinSubCategoryObject="%s"' % t["obj"]
    if t["container"]:
        s += ",ContainerType=%s" % t["container"]
    v = t.get("value")
    if t["container"] == "Map" and v:
        s += ",PinValueType=" + _terminal_text(v)
    return s


def _pin_text(name, t, direction, pin_id, default):
    s = 'CustomProperties Pin (PinId=%s,PinName="%s"' % (pin_id or gid(), name)
    if direction is not None and _direction(direction) == "EGPD_Output":
        s += ',Direction="EGPD_Output"'
    s += "," + _pin_type_attrs(t)
    if default is not None:
        s += ',DefaultValue="%s"' % default
    s += (",PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,"
          "bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,)")
    return s


def pin_line(name, type_str, direction=None, pin_id=None, default=None):
    """One `CustomProperties Pin (...)` line, as the engine exports it (no links)."""
    return _pin_text(name, parse_type(type_str), direction, pin_id, default)


def user_pin(name, type_str, direction):
    """`CustomProperties UserDefinedPin` line (function/event parameters). Entry nodes: inputs are 'out'."""
    return 'CustomProperties UserDefinedPin (PinName="%s",PinType=(%s),DesiredPinDirection=%s)' % (
        name, _compact_type(parse_type(type_str)), _direction(direction))


def var_line(name, type_str, guid=None, default=None, flags=5):
    """Value of one `NewVariables(i)=` line."""
    s = '(VarName="%s",VarGuid=%s,VarType=(%s),FriendlyName="%s",PropertyFlags=%d' % (
        name, guid or gid(), _compact_type(parse_type(type_str)), name, flags)
    if default is not None:
        s += ',DefaultValue="%s"' % default
    return s + ")"


def dispatcher_var(name, guid=None):
    return '(VarName="%s",VarGuid=%s,VarType=(PinCategory="mcdelegate"),FriendlyName="%s",PropertyFlags=%d)' % (
        name, guid or gid(), name, DISPATCHER_PROPERTY_FLAGS)


# ---- pins and nodes -----------------------------------------------------------

_PIN_PREFIX = "CustomProperties Pin ("
_NAME_RX = re.compile(r'PinName="((?:[^"\\]|\\.)*)"')
_ID_RX = re.compile(r"PinId=([0-9A-Fa-f]+)")
_LINK_RX = re.compile(r",LinkedTo=\(([^)]*)\)")
_DEFAULT_RX = re.compile(r',DefaultValue="(?:[^"\\]|\\.)*"')
_QSTR = r'"(?:[^"\\]|\\.)*"'
_DEFTEXT_RX = re.compile(r',DefaultTextValue=(?:NSLOCTEXT\(\s*%s\s*,\s*%s\s*,\s*%s\s*\)|INVTEXT\(\s*%s\s*\)|None)'
                         % (_QSTR, _QSTR, _QSTR, _QSTR))
_DEFOBJ_RX = re.compile(r',DefaultObject=(?:"(?:[^"\\]|\\.)*"|None)')
_PINTYPE_RX = re.compile(r'PinType\.PinCategory="[^"]*",PinType\.PinSubCategory="[^"]*",PinType\.PinSubCategoryObject=(?:"[^"]*"|None),'
                         r'PinType\.PinSubCategoryMemberReference=\([^)]*\),PinType\.PinValueType=\([^)]*\),PinType\.ContainerType=\w+')
_CAT_RX = re.compile(r'PinType\.PinCategory="([^"]*)"')
_CONTAINER_RX = re.compile(r'PinType\.ContainerType=(\w+)')
_CONTAINER_HINT = {"None": "a single-value pin (give a type without array:/map:)", "Array": "an array pin (give array:<type>)",
                   "Map": "a map pin (give map:<key>:<type>)", "Set": "a set pin (set types are not supported)"}
DEFAULT_WILDCARDS = {"K2Node_DynamicCast": {"Object": "object:/Script/CoreUObject.Object"}}
_POS_RX = {"x": re.compile(r"^NodePosX=(-?\d+)"), "y": re.compile(r"^NodePosY=(-?\d+)")}


def _insert_attr(text, attr):
    """Put `,attr` before ,PersistentGuid= (or before the closing parenthesis)."""
    i = text.find(",PersistentGuid=")
    if i >= 0:
        return text[:i] + "," + attr + text[i:]
    j = text.rstrip().rfind(")")
    return text[:j] + "," + attr + text[j:]


class Pin:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    @property
    def name(self):
        m = _NAME_RX.search(self.text)
        return m.group(1) if m else ""

    @property
    def direction(self):
        return "out" if 'Direction="EGPD_Output"' in self.text else "in"

    @property
    def id(self):
        m = _ID_RX.search(self.text)
        return m.group(1) if m else ""

    @property
    def category(self):
        m = _CAT_RX.search(self.text)
        return m.group(1) if m else ""

    @property
    def container(self):
        m = _CONTAINER_RX.search(self.text)
        return m.group(1) if m else "None"

    def is_wildcard(self):
        return self.category == "wildcard" or 'TerminalCategory="wildcard"' in self.text

    def set_type(self, t):
        if _PINTYPE_RX.search(self.text) is None:
            raise SpecError("cannot read the PinType of pin %r" % self.name)
        self.text = _PINTYPE_RX.sub(lambda m: _pin_type_core(t), self.text, count=1)

    def links(self):
        m = _LINK_RX.search(self.text)
        out = []
        if m:
            for e in m.group(1).split(","):
                e = e.strip()
                if e:
                    node, _, pid = e.partition(" ")
                    out.append((node, pid))
        return out

    def set_links(self, links):
        self.text = _LINK_RX.sub("", self.text, count=1)
        if links:
            self.text = _insert_attr(self.text, "LinkedTo=(" + "".join("%s %s," % l for l in links) + ")")


class GraphNode:
    def __init__(self, cls, name, head, pins):
        self.cls = cls
        self.name = name
        self.head = list(head)
        self.pins = list(pins)

    @classmethod
    def _split(klass, lines):
        head, pins = [], []
        for l in lines:
            s = l.strip()
            if s.startswith(_PIN_PREFIX):
                pins.append(Pin(s))
            elif s:
                head.append(s)
        return head, pins

    @classmethod
    def from_export(klass, cls, name, lines):
        head, pins = klass._split(lines)
        return klass(cls, name, head, pins)

    @classmethod
    def from_template(klass, cls, name, lines, pos):
        """Node from an engine-made template: new pin ids and NodeGuid, no position/links/compiler messages."""
        head, pins = [], []
        guid = None
        for l in lines:
            s = l.strip()
            if not s or s.startswith(("NodePosX", "NodePosY", "NodeGuid", "ErrorMsg=", "ErrorType=", "bHasCompilerMessage=")):
                continue
            if s.startswith(_PIN_PREFIX):
                s = _ID_RX.sub("PinId=" + gid(), s, count=1)
                s = _LINK_RX.sub("", s)
                if re.search(r'PinName="OutputDelegate"', s):
                    g = re.search(r"MemberGuid=([0-9A-Fa-f]{32})", s)
                    if g:
                        guid = g.group(1).upper()
                pins.append(Pin(s))
            else:
                head.append(s)
        head.append("NodeGuid=" + (guid or gid()))
        node = klass(cls, name, head, pins)
        node.move(pos[0], pos[1])
        return node

    # -- pins
    def pin(self, name, direction=None):
        matches = [p for p in self.pins if p.name == name]
        if direction is not None:
            d = "out" if _direction(direction) == "EGPD_Output" else "in"
            wrong = matches
            matches = [p for p in matches if p.direction == d]
            if not matches and wrong:
                raise SpecError("pin %r on %s is an %s pin; wires go from an output pin to an input pin"
                                % (name, self.name, "output" if wrong[0].direction == "out" else "input"))
        if len(matches) == 1:
            return matches[0]
        avail = ", ".join("%s(%s)" % (p.name, p.direction) for p in self.pins)
        if not matches:
            raise SpecError("no pin %r on %s (%s). Available: %s" % (name, self.name, self.cls.split(".")[-1], avail))
        raise SpecError("pin %r on %s is ambiguous (%s)" % (name, self.name, avail))

    def resolve_wildcard(self, pin, t):
        p = self.pin(pin)
        if not p.is_wildcard():
            raise SpecError("pin_types: pin %r on %s is not a wildcard pin (category %s); only wildcard pins take pin_types"
                            % (pin, self.name, p.category))
        if p.container != (t["container"] or "None"):
            raise SpecError("pin_types: pin %r on %s is %s" % (pin, self.name, _CONTAINER_HINT.get(p.container, p.container)))
        p.set_type(t)

    def fill_default_wildcards(self):
        """Type the wildcard pins whose type never depends on the wiring (bp_apply wires by text and the
        engine never propagates pin types)."""
        for pin, ts in DEFAULT_WILDCARDS.get(self.cls.rsplit(".", 1)[-1], {}).items():
            for p in self.pins:
                if p.name == pin and p.direction == "in" and p.is_wildcard():
                    p.set_type(parse_type(ts))

    def _resolve(self, pin, direction=None):
        return pin if isinstance(pin, Pin) else self.pin(pin, direction)

    def link(self, pin, other_name, other_pin_id):
        p = self._resolve(pin)
        links = p.links()
        if (other_name, other_pin_id) not in links:
            links.append((other_name, other_pin_id))
            p.set_links(links)

    def unlink(self, pin, other_name, other_pin_id=None):
        p = self._resolve(pin)
        p.set_links([l for l in p.links() if not (l[0] == other_name and (other_pin_id is None or l[1] == other_pin_id))])

    def drop_links_to(self, other_name):
        for p in self.pins:
            if any(l[0] == other_name for l in p.links()):
                self.unlink(p, other_name)

    def _input(self, pin):
        if isinstance(pin, Pin):
            return pin
        ins = [p for p in self.pins if p.name == pin and p.direction == "in"]
        return ins[0] if len(ins) == 1 else self.pin(pin)

    def set_default(self, pin, value):
        p = self._input(pin)
        p.text = _DEFAULT_RX.sub("", p.text)
        p.text = _insert_attr(p.text, 'DefaultValue="%s"' % value)
        if p.category == "text":
            # a text pin's value is its DefaultTextValue; DefaultValue alone imports as an empty FText
            p.text = _DEFTEXT_RX.sub("", p.text)
            if value != "":
                key = hashlib.md5(value.encode("utf-8")).hexdigest()[:32]
                p.text = _insert_attr(p.text, 'DefaultTextValue=NSLOCTEXT("","%s","%s")' % (key, value))

    def set_default_object(self, pin, object_text):
        p = self._input(pin)
        p.text = _DEFOBJ_RX.sub("", p.text)
        p.text = _insert_attr(p.text, 'DefaultObject="%s"' % object_text)
        # SpawnActor / Create Widget: the engine types ReturnValue from the Class pin, a text import does not
        if p.name == "Class" and self.cls.rsplit(".", 1)[-1] in ("K2Node_SpawnActorFromClass", "K2Node_CreateWidget"):
            for rv in self.pins:
                if rv.name == "ReturnValue" and rv.direction == "out":
                    rv.set_type({"cat": "object", "sub": "", "obj": object_text, "container": None})

    # -- position
    def pos(self):
        x = y = 0
        for l in self.head:
            m = _POS_RX["x"].match(l)
            if m:
                x = int(m.group(1))
            m = _POS_RX["y"].match(l)
            if m:
                y = int(m.group(1))
        return x, y

    def move(self, x, y):
        self.head = [l for l in self.head if not l.startswith(("NodePosX=", "NodePosY="))]
        self.head.append("NodePosX=%d" % int(x))
        self.head.append("NodePosY=%d" % int(y))

    def lines(self):
        return list(self.head) + [p.text for p in self.pins]

    def defn(self):
        return (self.cls, self.name, self.lines())


class GraphModel:
    """The nodes of one graph, edited as text and written back in one go."""

    def __init__(self):
        self.nodes = {}

    @classmethod
    def from_block(klass, block_text):
        decl, body, order, graph_lines = {}, {}, [], []
        depth, cur, isdef = 0, None, False
        for raw in block_text.split("\n"):
            s = raw.rstrip("\r").strip()
            if depth == 0:
                if s.startswith("Begin Object"):
                    m = re.match(r'Begin Object(?: Class=(\S+))? Name="([^"]+)"', s)
                    if not m:
                        raise SpecError("cannot parse graph line: " + s[:120])
                    c, n = m.group(1), m.group(2)
                    if c:
                        decl[n] = c
                    if n not in body:
                        body[n] = []
                        order.append(n)
                    cur, isdef, depth = n, c is None, 1
                elif s:
                    graph_lines.append(s)
            else:
                if s.startswith("Begin Object"):
                    depth += 1
                    if isdef:
                        body[cur].append(s)
                elif s == "End Object":
                    depth -= 1
                    if depth > 0 and isdef:
                        body[cur].append(s)
                elif s and isdef:
                    body[cur].append(s)
        wanted = []
        for l in graph_lines:
            m = re.match(r"Nodes\(\d+\)=(.*)$", l)
            if m:
                mm = re.search(r"'([^']+)'", m.group(1))
                ref = mm.group(1) if mm else m.group(1).strip('"')
                wanted.append(re.split(r"[.:]", ref)[-1])
        model = klass()
        for n in wanted:
            if n not in decl:
                raise SpecError("node %s is listed in the graph but has no declaration" % n)
            if decl[n].endswith("K2Node_Composite"):
                raise SpecError("graph holds a collapsed graph (K2Node_Composite %s); editing those is not supported" % n)
            model.add(GraphNode.from_export(decl[n], n, body.get(n, [])))
        return model

    def add(self, node):
        if node.name in self.nodes:
            raise SpecError("node name %s already exists in the graph" % node.name)
        self.nodes[node.name] = node
        return node

    def get(self, name):
        if name not in self.nodes:
            raise SpecError("no node %r in the graph (nodes: %s)" % (name, ", ".join(list(self.nodes)[:30])))
        return self.nodes[name]

    def remove(self, name):
        self.get(name)
        del self.nodes[name]
        for n in self.nodes.values():
            n.drop_links_to(name)

    def wire(self, a, pa, b, pb):
        na, nb = self.get(a), self.get(b)
        out = na.pin(pa, "out")
        inp = nb.pin(pb, "in")
        na.link(out, b, inp.id)
        nb.link(inp, a, out.id)

    def unwire(self, a, pa, b, pb):
        na, nb = self.get(a), self.get(b)
        out = na.pin(pa, "out")
        inp = nb.pin(pb, "in")
        na.unlink(out, b, inp.id)
        nb.unlink(inp, a, out.id)

    def unique_name(self, cls):
        short = cls.split(".")[-1]
        n = 9001
        while "%s_%d" % (short, n) in self.nodes:
            n += 1
        return "%s_%d" % (short, n)

    def defs(self):
        return [n.defn() for n in self.nodes.values()]


# ---- graph text ---------------------------------------------------------------

def graph_text(defs, indent=6):
    """Two-pass text of a graph's nodes (declare every object, then define it) with Schema and Nodes(i)."""
    pad = " " * indent
    out = []
    for c, n, _ in defs:
        out.append('%sBegin Object Class=%s Name="%s"' % (pad, c, n))
        out.append("%sEnd Object" % pad)
    for c, n, ls in defs:
        out.append('%sBegin Object Name="%s"' % (pad, n))
        out.extend(pad + "   " + l for l in ls)
        out.append("%sEnd Object" % pad)
    out.append('%sSchema="%s"' % (pad, SCHEMA_K2))
    for i, (c, n, _) in enumerate(defs):
        out.append("%sNodes(%d)=\"%s'%s'\"" % (pad, i, c, n))
    return "\n".join(out)


def graph_obj(name, defs, extra=(), guid=None):
    """A named graph with its nodes, to nest in a carrier Blueprint."""
    out = ['      Begin Object Class=/Script/Engine.EdGraph Name="%s"' % name, graph_text(defs, 9),
           "         GraphGuid=%s" % (guid or gid())]
    out.extend("         " + x for x in extra)
    out.append("      End Object")
    return "\n".join(out) + "\n"


def graph_block(full, name):
    """Declaration + definition text of a top-level graph from a Blueprint export (ExportPath stripped)."""
    ls = [l.rstrip("\r") for l in full.split("\n")]
    out = []
    key = 'Name="%s"' % name
    for k, l in enumerate(ls):
        if l.startswith("   Begin Object") and key in l:
            j = k + 1
            while j < len(ls) and not ls[j].startswith("   End Object"):
                j += 1
            out += ls[k + 1:j]
    return "\n".join(re.sub(r' ExportPath="[^"]*"', "", l) for l in out)


def tree_block(full, asset_name):
    """Text of a Widget Blueprint's WidgetTree from its export."""
    ls = [l.rstrip("\r") for l in full.split("\n")]
    blocks = []
    for k, l in enumerate(ls):
        if l.startswith("   Begin Object") and 'Name="WidgetTree"' in l:
            j = k + 1
            while j < len(ls) and not ls[j].startswith("   End Object"):
                j += 1
            blocks.append(ls[k + 1:j])
    out = [re.sub(r' (ExportPath|Archetype)="[^"]*"', "", l).replace(asset_name + ":WidgetTree.", "") for b in blocks for l in b]
    return "\n".join(out)


_SCRATCH_REF = re.compile(r"/Script/[A-Za-z.]*GeneratedClass'[^']*BP_BkScratch\d+\.BP_BkScratch\d+_C'")
_SCRATCH_BARE = re.compile(r"/[A-Za-z0-9_/]*BP_BkScratch\d+\.BP_BkScratch\d+_C")


def retarget(lines, class_text):
    """Point template references to the throwaway scratch class at the real generated class.
    class_text is e.g. /Script/Engine.BlueprintGeneratedClass'/Mod/BP_X.BP_X_C'."""
    bare = class_text.split("'")[1] if "'" in class_text else class_text
    return [_SCRATCH_BARE.sub(lambda m: bare, _SCRATCH_REF.sub(lambda m: class_text, l)) for l in lines]


# ---- node builders ---------------------------------------------------------------

def _fref(fn, parent):
    return 'FunctionReference=(%sMemberName="%s")' % ('MemberParent="%s",' % parent if parent else "", fn)


def entry_node(fn, inputs, flags=FLAGS_FN, parent=None, editable=True, pos=(0, 0), exec_pin=True, name="K2Node_FunctionEntry_0"):
    head = [_fref(fn, parent)]
    if editable:
        head.append("bIsEditable=True")
    head.append("ExtraFlags=%d" % flags)
    if editable:
        head += [user_pin(i["name"], i["type"], "out") for i in inputs]
    head += ["NodeGuid=" + gid(), "NodePosX=%d" % pos[0], "NodePosY=%d" % pos[1]]
    pins = []
    if exec_pin:
        pins.append(Pin(pin_line("then", "exec", "out")))
    pins += [Pin(pin_line(i["name"], i["type"], "out")) for i in inputs]
    return GraphNode(ENTRY_CLASS, name, head, pins)


def result_node(fn, outputs, parent=None, pos=(900, 0), exec_pin=True, name="K2Node_FunctionResult_0"):
    head = [_fref(fn, parent), "bIsEditable=True"]
    if parent is None:
        head += [user_pin(o["name"], o["type"], "in") for o in outputs]
    head += ["NodeGuid=" + gid(), "NodePosX=%d" % pos[0], "NodePosY=%d" % pos[1]]
    pins = []
    if exec_pin:
        pins.append(Pin(pin_line("execute", "exec")))
    pins += [Pin(pin_line(o["name"], o["type"])) for o in outputs]
    return GraphNode(RESULT_CLASS, name, head, pins)


def tunnel_nodes(inputs, outputs):
    """Macro entry and exit tunnels. inputs/outputs are [{"name","type"}] (exec pins have type exec)."""
    a = GraphNode(TUNNEL_CLASS, "K2Node_Tunnel_0",
                  ["bCanHaveOutputs=True"] + [user_pin(i["name"], i["type"], "out") for i in inputs]
                  + ["NodeGuid=" + gid(), "NodePosX=0", "NodePosY=0"],
                  [Pin(pin_line(i["name"], i["type"], "out")) for i in inputs])
    b = GraphNode(TUNNEL_CLASS, "K2Node_Tunnel_1",
                  ["bCanHaveInputs=True"] + [user_pin(o["name"], o["type"], "in") for o in outputs]
                  + ["NodeGuid=" + gid(), "NodePosX=600", "NodePosY=0"],
                  [Pin(pin_line(o["name"], o["type"])) for o in outputs])
    return a, b


def self_node(name, pos):
    t = {"cat": "object", "sub": "self", "obj": None, "container": None}
    return GraphNode(SELF_CLASS, name, ["NodeGuid=" + gid(), "NodePosX=%d" % pos[0], "NodePosY=%d" % pos[1]],
                     [Pin(_pin_text("self", t, "out", None, None))])


def macro_instance_node(name, bp_path, graph_guid, inputs, outputs, pos):
    head = ["MacroGraphReference=(GraphBlueprint=\"/Script/Engine.Blueprint'%s'\",GraphGuid=%s)" % (bp_path, graph_guid),
            "NodeGuid=" + gid(), "NodePosX=%d" % pos[0], "NodePosY=%d" % pos[1]]
    pins = [Pin(pin_line(i["name"], i["type"])) for i in inputs] + [Pin(pin_line(o["name"], o["type"], "out")) for o in outputs]
    return GraphNode(MACRO_CLASS, name, head, pins)


def dispatcher_text(name, params):
    """(variable line, signature graph text) of an event dispatcher."""
    entry = entry_node(name, params, flags=FLAGS_DELEGATE)
    return dispatcher_var(name), graph_obj(name, [entry.defn()])


def timeline_text(spec, guid=None):
    """(text of the TimelineTemplate object, template name, guid). One CurveFloat per track."""
    name = spec["name"]
    tn = name + "_Template"
    guid = guid or gid()
    tracks = spec.get("tracks") or []
    if not tracks:
        raise SpecError("timeline %s needs at least one track" % name)
    out = ['      Begin Object Class=/Script/Engine.TimelineTemplate Name="%s"' % tn]
    for t in tracks:
        cn = "%s_%s_Curve" % (name, t["name"])
        keys = ",".join("(Time=%f,Value=%f)" % (float(k[0]), float(k[1])) for k in t.get("keys") or [])
        out.append('         Begin Object Class=/Script/Engine.CurveFloat Name="%s"' % cn)
        out.append("            FloatCurve=(Keys=(%s))" % keys)
        out.append("         End Object")
    out.append("         TimelineLength=%f" % float(spec.get("length", 1.0)))
    if spec.get("loop"):
        out.append("         bLoop=True")
    for i, t in enumerate(tracks):
        cn = "%s_%s_Curve" % (name, t["name"])
        out.append("         FloatTracks(%d)=(TrackName=\"%s\",CurveFloat=\"/Script/Engine.CurveFloat'%s'\")" % (i, t["name"], cn))
    out.append("         TimelineGuid=%s" % guid)
    out.append("      End Object")
    return "\n".join(out) + "\n", tn, guid


def animation_text(spec, widget_classes):
    """Text of a WidgetAnimation with its MovieScene (same name) and one possessable per animated widget.
    widget_classes: {widget name: class path}. Keys are added afterwards through Sequencer Python."""
    name = spec["name"]
    guids = {}
    for t in spec.get("tracks") or []:
        w = t["widget"]
        if w not in widget_classes:
            raise SpecError("animation %s: no widget %r in the widget tree (%s)" % (name, w, ", ".join(sorted(widget_classes))))
        guids.setdefault(w, gid())
    out = ['      Begin Object Class=/Script/UMG.WidgetAnimation Name="%s"' % name,
           '         Begin Object Class=/Script/MovieScene.MovieScene Name="%s"' % name]
    for i, (w, g) in enumerate(guids.items()):
        out.append('            Possessables(%d)=(Guid=%s,Name="%s",PossessedObjectClass="%s")' % (i, g, w, class_ref(widget_classes[w])))
    for i, (w, g) in enumerate(guids.items()):
        out.append('            ObjectBindings(%d)=(ObjectGuid=%s,BindingName="%s")' % (i, g, w))
    out.append("         End Object")
    out.append("         MovieScene=\"/Script/MovieScene.MovieScene'%s'\"" % name)
    for i, (w, g) in enumerate(guids.items()):
        out.append('         AnimationBindings(%d)=(WidgetName="%s",AnimationGuid=%s,bIsRootWidget=False)' % (i, w, g))
    out.append("      End Object")
    return "\n".join(out) + "\n"


# ---- widget tree ---------------------------------------------------------------

SLOT_CLASS = {"VerticalBox": "VerticalBoxSlot", "HorizontalBox": "HorizontalBoxSlot", "CanvasPanel": "CanvasPanelSlot",
              "Overlay": "OverlaySlot", "Border": "BorderSlot", "Button": "ButtonSlot", "SizeBox": "SizeBoxSlot",
              "ScrollBox": "ScrollBoxSlot", "WrapBox": "WrapBoxSlot", "ScaleBox": "ScaleBoxSlot",
              "WidgetSwitcher": "WidgetSwitcherSlot", "GridPanel": "GridSlot", "UniformGridPanel": "UniformGridSlot"}


def _widget_class(c):
    if not isinstance(c, str) or not c:
        raise SpecError("widget needs a class")
    return c if c.startswith("/") else "/Script/UMG." + c


def widget_tree_text(root):
    """Nested spec {"class","name","children":[...],"text","slot":{},"props":{}} -> (two-pass text, root name)."""
    flat = []          # (spec, parent spec or None, index in parent)

    def walk(w, parent, idx):
        if not isinstance(w, dict) or not w.get("name"):
            raise SpecError("every widget needs a name")
        if not re.match(r"^[A-Za-z0-9_]+$", w["name"]):
            raise SpecError("widget name %r may only hold letters, digits and underscores" % w["name"])
        _widget_class(w.get("class"))
        flat.append((w, parent, idx))
        kids = w.get("children") or []
        if kids and _widget_class(w["class"]).split(".")[-1] not in SLOT_CLASS:
            raise SpecError("widget %s (%s) cannot have children" % (w["name"], w["class"]))
        for i, k in enumerate(kids):
            walk(k, w, i)

    walk(root, None, 0)
    names = [f[0]["name"] for f in flat]
    if len(set(names)) != len(names):
        raise SpecError("duplicate widget names: %s" % sorted({n for n in names if names.count(n) > 1}))

    def ref(cls, nm):
        return "%s'%s'" % (cls, nm)

    def slot_name(parent, i):
        return "%s_Slot%d" % (parent["name"], i)

    def slot_cls(parent):
        return "/Script/UMG." + SLOT_CLASS[_widget_class(parent["class"]).split(".")[-1]]

    out = []
    for w, _, _ in flat:
        cls = _widget_class(w["class"])
        out.append('      Begin Object Class=%s Name="%s"' % (cls, w["name"]))
        for i, _k in enumerate(w.get("children") or []):
            out.append('         Begin Object Class=%s Name="%s"' % (slot_cls(w), slot_name(w, i)))
            out.append("         End Object")
        out.append("      End Object")
    for w, parent, idx in flat:
        cls = _widget_class(w["class"])
        out.append('      Begin Object Name="%s"' % w["name"])
        kids = w.get("children") or []
        for i, k in enumerate(kids):
            out.append('         Begin Object Name="%s"' % slot_name(w, i))
            for prop, val in (k.get("slot") or {}).items():
                out.append("            %s=%s" % (prop, val))
            out.append('            Parent="%s"' % ref(cls, w["name"]))
            out.append('            Content="%s"' % ref(_widget_class(k["class"]), k["name"]))
            out.append("         End Object")
        for i, _k in enumerate(kids):
            out.append('         Slots(%d)="%s"' % (i, ref(slot_cls(w), slot_name(w, i))))
        if w.get("text") is not None:
            out.append('         Text=NSLOCTEXT("Mcp", "%s", "%s")' % (w["name"], str(w["text"]).replace('"', '\\"')))
        for prop, val in (w.get("props") or {}).items():
            out.append("         %s=%s" % (prop, val))
        if parent is not None:
            out.append('         Slot="%s"' % ref(slot_cls(parent), "%s.%s" % (parent["name"], slot_name(parent, idx))))
        out.append("         bIsVariable=True")
        out.append('         DisplayLabel="%s"' % w["name"])
        out.append("      End Object")
    out.append('      RootWidget="%s"' % ref(_widget_class(root["class"]), root["name"]))
    return "\n".join(out) + "\n", root["name"]
