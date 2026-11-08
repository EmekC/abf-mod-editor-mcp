"""Pure text helpers of the reflect op (no `unreal` import, so the host and the unit tests can use them).

Pins come from `CustomProperties Pin (...)` lines of a node exported from a scratch Blueprint; bodies are the
property lines of the node templates that make_templates asks the engine to complete.
"""
import re
import uuid

from . import t3d

_PIN_LINE = re.compile(r"CustomProperties Pin \((.*)\)\s*$")


def parse_attrs(s):
    """Split 'k=v,k=v,...' at top level (quotes and parentheses respected) into a dict of raw value strings."""
    out = {}
    i = 0
    while i < len(s):
        eq = s.find("=", i)
        if eq < 0:
            break
        key = s[i:eq]
        j, depth, inq = eq + 1, 0, False
        while j < len(s):
            c = s[j]
            if inq:
                if c == "\\":
                    j += 1
                elif c == '"':
                    inq = False
            elif c == '"':
                inq = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 0:
                break
            j += 1
        out[key] = s[eq + 1:j]
        i = j + 1
    return out


def _unq(v):
    if v is None:
        return None
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _object_parts(v):
    """PinSubCategoryObject value -> (inner path, outer class path). 'None' or empty -> (None, None)."""
    v = _unq(v)
    if not v or v == "None":
        return None, None
    m = re.match(r"^(.*?)'([^']*)'$", v)
    if m:
        return m.group(2), m.group(1)
    return v, None


def parse_pins(lines):
    """Pins of every `CustomProperties Pin (...)` line in `lines` (an iterable of strings)."""
    pins = []
    for line in lines:
        m = _PIN_LINE.search(line.strip())
        if not m:
            continue
        a = parse_attrs(m.group(1))
        inner, meta = _object_parts(a.get("PinType.PinSubCategoryObject"))
        default = _unq(a.get("DefaultValue"))
        pins.append({
            "name": _unq(a.get("PinName")),
            "dir": "out" if _unq(a.get("Direction")) == "EGPD_Output" else "in",
            "category": _unq(a.get("PinType.PinCategory")),
            "subCategory": _unq(a.get("PinType.PinSubCategory")) or "",
            "subCategoryObject": inner,
            "objectMeta": meta,
            "container": a.get("PinType.ContainerType", "None"),
            "hidden": a.get("bHidden") == "True",
            "default": default if default != "" else None,
        })
    return pins


def class_pin_line(path, meta):
    """The `Class` input pin line of a K2Node_GetClassDefaults, with DefaultObject naming `path`
    (`meta` is the class of the class object, e.g. /Script/Engine.BlueprintGeneratedClass or /Script/CoreUObject.Class)."""
    return ('CustomProperties Pin (PinId=%s,PinName="Class",PinType.PinCategory="class",PinType.PinSubCategory="",'
            'PinType.PinSubCategoryObject="/Script/CoreUObject.Class\'/Script/CoreUObject.Object\'",PinType.PinSubCategoryMemberReference=(),'
            'PinType.PinValueType=(),PinType.ContainerType=None,PinType.bIsReference=False,PinType.bIsConst=False,PinType.bIsWeakPointer=False,'
            'PinType.bIsUObjectWrapper=False,PinType.bSerializeAsSinglePrecisionFloat=False,DefaultObject="%s\'%s\'",'
            'PersistentGuid=00000000000000000000000000000000,bHidden=False,bNotConnectable=False,bDefaultValueIsReadOnly=False,'
            'bDefaultValueIsIgnored=False,bAdvancedView=False,bOrphanedPin=False,)') % (uuid.uuid4().hex.upper(), meta, path)


def call_body(class_text, fn):
    return ['FunctionReference=(MemberParent="%s",MemberName="%s")' % (class_text, fn)]


def get_body(class_text, prop):
    return ['VariableReference=(MemberParent="%s",MemberName="%s")' % (class_text, prop)]


def break_body(struct_path):
    kind = "/Script/CoreUObject.ScriptStruct" if struct_path.startswith("/Script/") else "/Script/Engine.UserDefinedStruct"
    return ["bMadeAfterOverridePinRemoval=True", 'StructType="%s"' % t3d.class_ref(struct_path, kind)]


def cast_body(class_text):
    return ['TargetType="%s"' % class_text]


def cast_pin(pins):
    """Name of the cast result output pin: the output that is neither an exec pin nor bSuccess."""
    for p in pins:
        if p["dir"] == "out" and p["category"] != "exec" and p["name"] != "bSuccess":
            return p["name"]
    return None


def SKIP_FUNCTION(name):
    return name.startswith("ExecuteUbergraph_") or name.endswith("__DelegateSignature")


META_BUDGET_SECONDS = 20.0


DEFAULT_PREFIX = "CPP_Default_"


LATENT_PIN = "LatentInfo"   # UHT writes meta LatentInfo="LatentInfo" (key == value == pin name): a marker, not a default


def read_function_defaults(read_tag, fn, read_all=None, pins=None):
    """{pin: default text} of a UFunction's input pins, or None when unreadable. The lookup is Unreal's own
    (UEdGraphSchema_K2::FindFunctionParameterDefaultValue): the metadata key equal to the PARAMETER NAME first (UHT
    `meta=(Duration="0.2")`), then CPP_Default_<Param> (the C++ default argument); empty values are no default.
    read_all(fn) -> {tag: value} reads every tag at once (unreal.EditorAssetLibrary.get_metadata_tag_values). With `pins` both
    keys are looked up for each pin; without `pins` only the CPP_Default_ keys can be listed (a parameter-name key cannot be told from
    other metadata). When read_all is missing or raises, each of `pins` is read with read_tag(fn, pin), then
    read_tag(fn, "CPP_Default_" + pin) (first non-empty wins; unreadable = None when no value was found and no CPP_Default_ read worked)."""
    if read_all is not None:
        try:
            tags = {str(k): str(v) for k, v in dict(read_all(fn)).items()}
            if pins is None:
                return {k[len(DEFAULT_PREFIX):]: v for k, v in tags.items() if k.startswith(DEFAULT_PREFIX) and v != ""}
            got = {}
            for pin in pins:
                v = (tags.get(pin) if pin != LATENT_PIN else None) or tags.get(DEFAULT_PREFIX + pin)
                if v:
                    got[pin] = v
            return got
        except Exception:
            pass
    if not pins:
        return None
    got, readable = {}, False
    for pin in pins:
        for key in (pin, DEFAULT_PREFIX + pin) if pin != LATENT_PIN else (DEFAULT_PREFIX + pin,):
            try:
                v = read_tag(fn, key)
            except Exception:
                continue
            v = str(v) if v is not None else ""
            if v != "":
                got[pin] = v
                readable = True
                break
            if key.startswith(DEFAULT_PREFIX):
                readable = True   # the C++ default read worked and found none (an empty parameter-name read proves nothing)
    return got if readable else None


def read_function_meta(read_tag, fn, read_all=None, pins=None):
    """{"tooltip": str|None, "displayName": str|None, "defaults": {pin: text}|None} of one UFunction; read_tag(fn, tag) -> str
    (the engine route is unreal.EditorAssetLibrary.get_metadata_tag). A failing or empty read gives None."""
    out = {}
    for key, tag in (("tooltip", "ToolTip"), ("displayName", "DisplayName")):
        try:
            v = read_tag(fn, tag)
            v = str(v).strip() if v is not None else ""
            out[key] = v or None
        except Exception:
            out[key] = None
    out["defaults"] = read_function_defaults(read_tag, fn, read_all, pins)
    return out


def collect_function_meta(read_tag, items, budget=META_BUDGET_SECONDS, clock=None, read_all=None, pins=None):
    """items: [(key, fn)] -> {key: meta}; pins: {key: [input pin names]} for the per-pin default fallback. Once the whole
    pass has run longer than `budget` seconds the remaining functions get a meta with every field None."""
    import time
    clock = clock or time.monotonic
    start = clock()
    out = {}
    for key, fn in items:
        if clock() - start > budget:
            out[key] = {"tooltip": None, "displayName": None, "defaults": None}
        else:
            out[key] = read_function_meta(read_tag, fn, read_all, (pins or {}).get(key))
    return out


OBJECT_PATH = "/Script/CoreUObject.Object"


def verified_supers(path, names, is_child):
    """Ancestor paths of `path` (nearest first, e.g. from the asset registry) when the whole chain checks out, else None.
    The self entry and duplicates are dropped; the chain must end at /Script/CoreUObject.Object and every link
    (`path` -> names[0], names[i] -> names[i+1]) must satisfy is_child(child, parent). An is_child that raises gives None."""
    chain = []
    for n in names or []:
        if n != path and n not in chain:
            chain.append(n)
    if not chain or chain[-1] != OBJECT_PATH:
        return None
    try:
        if not is_child(path, chain[0]):
            return None
        for i in range(len(chain) - 1):
            if not is_child(chain[i], chain[i + 1]):
                return None
    except Exception:
        return None
    return chain


# ---- member discovery (widgets, components and plain object variables that GetClassDefaults cannot list) ----

# characters Unreal refuses in an object name (INVALID_OBJECTNAME_CHARACTERS): such a name is never a member
INVALID_MEMBER_CHARS = frozenset("\"',/.:|&!~\n\r\t@#(){}[]=;^%$`")
GEN_VARIABLE = "_GEN_VARIABLE"
WIDGET_TREE = "WidgetTree."


def member_name_ok(name):
    return isinstance(name, str) and name != "" and INVALID_MEMBER_CHARS.isdisjoint(name)


def group_member_paths(object_paths, wanted=None):
    """({class path: widget names}, {class path: SCS variable names}) from object path names:
    `<class>:WidgetTree.<N>` and `<class>:<N>_GEN_VARIABLE` (N without `.` or `:`). `wanted` restricts the class paths."""
    widgets, scs = {}, {}
    for p in object_paths:
        head, sep, tail = p.partition(":")
        if not sep or (wanted is not None and head not in wanted):
            continue
        if tail.startswith(WIDGET_TREE):
            n, into = tail[len(WIDGET_TREE):], widgets
        elif tail.endswith(GEN_VARIABLE):
            n, into = tail[:-len(GEN_VARIABLE)], scs
        else:
            continue
        if n and "." not in n and ":" not in n:
            into.setdefault(head, set()).add(n)
    return widgets, scs


def widget_tree_names(object_paths, class_path):
    return group_member_paths(object_paths, {class_path})[0].get(class_path, set())


def scs_names(object_paths, class_path):
    return group_member_paths(object_paths, {class_path})[1].get(class_path, set())


def probe_property(get, name):
    """(True, value) when get(name) works, else (False, None). `get` is a CDO's get_editor_property: it raises for a name the
    class does not have (and for functions), so this is the exact existence check."""
    try:
        return True, get(name)
    except Exception:
        return False, None


def has_property(get, name):
    return probe_property(get, name)[0]


def is_object_value(value, object_type):
    """True for a value an object reference can hold: None (unset) or an instance of `object_type`."""
    return value is None or isinstance(value, object_type)


def member_pin(pins, name):
    """The single-object output pin of a VariableGet node named `name` (case-insensitive), else None."""
    low = name.lower()
    for p in pins or []:
        if p.get("dir") == "out" and p.get("category") == "object" and p.get("container") == "None" and not p.get("hidden") \
                and str(p.get("name") or "").lower() == low:
            return p
    return None


def member_candidates(widget, scs, other, known_lower=()):
    """{lower: (name, kind)} from three name sets; a name found by several sources keeps the first kind of widget > component > member.
    Names in `known_lower` and names that cannot be a member are dropped."""
    out = {}
    for names, kind in ((widget, "widget"), (scs, "component"), (other, "member")):
        for n in sorted(names or ()):
            low = n.lower()
            if low in out or low in known_lower or not member_name_ok(n):
                continue
            out[low] = (n, kind)
    return out


_DOC_PROPERTY = re.compile(r"- ``([A-Za-z0-9_]+)`` \(")


def doc_property_names(doc):
    """Editor property names (snake_case, in order, unique) listed in the docstring of a native class's Python wrapper."""
    seen, out = set(), []
    for n in _DOC_PROPERTY.findall(doc or ""):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def snake_to_pascal(name):
    return "".join(w[:1].upper() + w[1:] for w in name.split("_"))
