"""Editor-side primitives (needs `unreal`): carrier import, raw TArray swaps, export.

Python cannot create Blueprint nodes and cannot read or write several Blueprint arrays, so text is imported into a
throwaway carrier object and the carrier's TArray triples (pointer, count, max) are swapped with the real object's in
process memory. Offsets are discovered per class at run time by importing a marker-sized array; anything ambiguous
or mismatched raises instead of guessing.
"""
import ctypes
import random
import re
import string
import struct

import unreal

LIB = unreal.BlueprintEditorLibrary


class _Cfg:
    dest = None       # /<Mod>/_McpTmp : where carriers and scratch Blueprints live (never saved)
    tmpdir = None     # run folder


CFG = _Cfg()


def configure(dest, tmpdir):
    CFG.dest = dest
    CFG.tmpdir = str(tmpdir).replace("\\", "/")


def tools():
    return unreal.AssetToolsHelpers.get_asset_tools()


def must(value, what):
    """Guard load results: using None crashes the editor."""
    if value is None:
        raise RuntimeError("%s is None" % what)
    return value


def addr(o):
    return int(re.search(r"\(0x([0-9A-Fa-f]+)\)", str(o)).group(1), 16)


def rd(a, n):
    return ctypes.string_at(a, n)


def pth(o):
    return str(o).split("'")[1]


def swap(a, b, n):
    x, y = rd(a, n), rd(b, n)
    ctypes.memmove(a, y, n)
    ctypes.memmove(b, x, n)


def carry(inner, base, cls):
    """Import `inner` (object text containing @@NAME@@) as a nested object of a throwaway actor in a Map import.
    Actor names get renumbered and some fail silently: random name, find the result by a unique sentinel, retry."""
    tok = "".join(random.choices(string.ascii_lowercase, k=6))
    want = base + "_" + tok
    text = inner.replace("@@NAME@@", want)
    for _ in range(4):
        an = "Z" + "".join(random.choices(string.ascii_lowercase, k=5))
        txt = ("Begin Map\nBegin Level\nBegin Actor Class=/Script/Engine.Actor Name=" + an + "\n" + text +
               "\nEnd Actor\nEnd Level\nEnd Map\n")
        p = CFG.tmpdir + "/bk_import.t3d"
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
        t = unreal.AssetImportTask()
        t.filename = p
        t.destination_path = CFG.dest
        t.destination_name = "BkDummy" + an
        t.automated = True
        t.replace_existing = True
        t.save = False
        t.factory = unreal.LevelFactory()
        tools().import_asset_tasks([t])
        hit = [o for o in unreal.ObjectIterator(cls) if pth(o).endswith("." + want)]
        if hit:
            return hit[-1]
    raise RuntimeError("carrier import produced nothing: " + want)


def nodes_of(graph):
    p = pth(graph) + "."
    return [n for n in unreal.ObjectIterator(unreal.EdGraphNode) if pth(n).startswith(p) and "." not in pth(n)[len(p):]]


def nodes_array(g, node_addrs, n=None):
    """(offset, [node pointers]) of the TArray<UEdGraphNode*> inside graph g, or None. With n, the count must be n;
    without, any count from 1 to len(node_addrs). Every element must be a known node. Raises when ambiguous."""
    b = rd(addr(g), 0x180)
    hits = []
    for o in range(0x28, 0x170, 8):
        p, c, m = struct.unpack_from("<Qii", b, o)
        if c <= 0 or m < c or not (0x10000 < p < 0x7fffffffffff) or p % 8:
            continue
        if n is not None and c != n:
            continue
        if n is None and c > len(node_addrs):
            continue
        ptrs = struct.unpack("<%dQ" % c, rd(p, 8 * c))
        if all(x in node_addrs for x in ptrs):
            hits.append((o, list(ptrs)))
    if len(hits) > 1:
        raise RuntimeError("Nodes array offset ambiguous: %s" % [h[0] for h in hits])
    return hits[0] if hits else None


def find_nodes_off(g, node_addrs, n):
    r = nodes_array(g, node_addrs, n)
    return r[0] if r else None


def export_text(o, name="bk_exp.t3d"):
    fn = CFG.tmpdir + "/" + name
    e = unreal.AssetExportTask()
    e.object = o
    e.filename = fn
    e.exporter = unreal.ObjectExporterT3D()
    e.automated = True
    e.replace_identical = True
    e.prompt = False
    if not unreal.Exporter.run_asset_export_task(e):
        raise RuntimeError("export failed")
    with open(fn, encoding="utf-8-sig") as f:
        return f.read()


def bpcls(bp):
    return "/Script/UMGEditor.WidgetBlueprint" if bp.get_class().get_name() == "WidgetBlueprint" else "/Script/Engine.Blueprint"


_ARR = {}


def find_vars_off(bp):
    k = (bp.get_class().get_name(), "NewVariables")
    if k in _ARR:
        return _ARR[k]
    n = 23
    ls = "".join('      NewVariables(%d)=(VarName="P%d",VarGuid=%s,VarType=(PinCategory="int"))\n' % (i, i, "%032X" % (i + 1)) for i in range(n))
    ib = carry('   Begin Object Class=%s Name="@@NAME@@"\n%s   End Object' % (bpcls(bp), ls), "BkProbe", unreal.Blueprint)
    b = rd(addr(ib), 0x600)
    hits = []
    for o in range(0x28, 0x5f0, 8):
        p, c, m = struct.unpack_from("<Qii", b, o)
        if c == n and m >= c and 0x10000 < p < 0x7fffffffffff:
            hits.append(o)
    if len(hits) != 1:
        raise RuntimeError("NewVariables offset ambiguous %s" % hits)
    _ARR[k] = hits[0]
    return hits[0]


def bp_arr_off(bp, prop, elem, inner=""):
    """Offset of a Blueprint TArray property, found by importing 29 copies of `elem` and looking for count 29."""
    k = (bp.get_class().get_name(), prop)
    if k in _ARR:
        return _ARR[k]
    n = 29
    ls = "".join("      %s(%d)=%s\n" % (prop, i, elem) for i in range(n))
    ib = carry('   Begin Object Class=%s Name="@@NAME@@"\n%s%s   End Object' % (bpcls(bp), inner, ls), "BkArr", unreal.Blueprint)
    b = rd(addr(ib), 0x700)
    hits = []
    for o in range(0x28, 0x6f0, 8):
        p, c, m = struct.unpack_from("<Qii", b, o)
        if c == n and m >= c and 0x10000 < p < 0x7fffffffffff:
            hits.append(o)
    if len(hits) != 1:
        raise RuntimeError("%s offset ambiguous %s" % (prop, hits))
    _ARR[k] = hits[0]
    return hits[0]


def array_append(bp, prop, off, inner_text, element_lines, stride=8):
    """Append elements to a Blueprint array without losing the existing ones.
    The imported array is sized old+new, the old elements are copied after the new ones, the arrays are swapped and the
    carrier's count is zeroed so it frees only its buffer. element_lines: text of each new element (right of '=');
    inner_text: the objects those elements refer to, nested in the carrier. Returns the carrier."""
    p0, c0, _m = struct.unpack_from("<Qii", rd(addr(bp) + off, 16))
    n = len(element_lines)
    total = c0 + n
    ls = "".join("      %s(%d)=%s\n" % (prop, i, element_lines[min(i, n - 1)]) for i in range(total))
    ib = carry('   Begin Object Class=%s Name="@@NAME@@"\n%s%s   End Object' % (bpcls(bp), inner_text, ls), "BkAdd", unreal.Blueprint)
    pi, ci, _m2 = struct.unpack_from("<Qii", rd(addr(ib) + off, 16))
    if ci != total:
        raise RuntimeError("array import %s: %d of %d elements" % (prop, ci, total))
    if c0:
        ctypes.memmove(pi + stride * n, rd(p0, stride * c0), stride * c0)
    swap(addr(ib) + off, addr(bp) + off, 16)
    ctypes.memmove(addr(ib) + off + 8, struct.pack("<i", 0), 4)
    return ib


def adopt_graphs(carrier, names, bp):
    """Move the named graphs imported under `carrier` into the real Blueprint."""
    pre = pth(carrier) + "."
    out = []
    for g in list(unreal.ObjectIterator(unreal.EdGraph)):
        p = pth(g)
        if p.startswith(pre) and "." not in p[len(pre):] and g.get_name() in names:
            g.rename(None, bp)
            out.append(g)
    if len(out) != len(names):
        raise RuntimeError("adopted %d of %d graphs (%s)" % (len(out), len(names), names))
    return out


def gen_class_text(bp):
    """T3D reference text of a Blueprint's generated class."""
    gc = must(LIB.generated_class(bp), "generated class of " + pth(bp))
    meta = {"WidgetBlueprintGeneratedClass": "/Script/UMG.WidgetBlueprintGeneratedClass"}.get(
        gc.get_class().get_name(), "/Script/Engine.BlueprintGeneratedClass")
    return "%s'%s'" % (meta, pth(gc))
