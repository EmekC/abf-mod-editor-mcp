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
