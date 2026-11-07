"""Editor-side UserDefinedStruct creation: the members cannot be edited through Python, so they are authored on a
throwaway struct (never saved) by swapping a carrier's VariablesDescriptions TArray into its editor data, and the
throwaway is duplicated to the final path (duplicating a UserDefinedStruct compiles the copy with exactly those
members; the swap alone does not rebuild). Nothing is ever deleted. Existing structs are compared, never replaced."""
import struct
import uuid

import unreal

from . import core, t3d
from .core import must, pth

ED_CLS = "/Script/UnrealEd.UserDefinedStructEditorData"
_OFF = {}


def descriptions_offset():
    """Offset of VariablesDescriptions in UserDefinedStructEditorData, found by importing 29 members and looking for count 29."""
    if "off" in _OFF:
        return _OFF["off"]
    n = 29
    e = '(VarName="A_0_%s",VarGuid=%s,FriendlyName="A",Category="int")' % ("0" * 32, "0" * 32)
    c = carrier([e] * n)
    b = core.rd(core.addr(c), 0x300)
    hits = []
    for o in range(0x28, 0x2f0, 8):
        p, cnt, mx = struct.unpack_from("<Qii", b, o)
        if cnt == n and mx >= n and 0x10000 < p < 0x7fffffffffff:
            hits.append(o)
    if len(hits) != 1:
        raise RuntimeError("VariablesDescriptions offset ambiguous %s" % hits)
    _OFF["off"] = hits[0]
    return hits[0]


def carrier(lines):
    """A throwaway UserDefinedStructEditorData whose VariablesDescriptions are `lines` (element texts)."""
    ls = "".join("      VariablesDescriptions(%d)=%s\n" % (i, e) for i, e in enumerate(lines))
    return core.carry('   Begin Object Class=%s Name="@@NAME@@"\n%s   End Object' % (ED_CLS, ls), "BkEd", unreal.Object)


def signature_of(struct_obj):
    """Ordered [(VarName, type)] of a loaded struct, read from its export."""
    return t3d.struct_signature(core.export_text(struct_obj, "struct_%s.t3d" % struct_obj.get_name()))


def set_fields(struct_path, fields):
    """Create the struct at `struct_path` with `fields` [{name, friendly, guid, type}]. Returns (object, report); the object
    is None when the struct already exists with the same members (nothing to save). Different members raise."""
    if not struct_path.startswith("/") or struct_path.startswith("/Game/") or struct_path.startswith("/Script/"):
        raise RuntimeError("refusing to write " + struct_path)
    lines = [t3d.struct_var_line(f) for f in fields]
    want = t3d.struct_signature("\n".join(lines))
    name = struct_path.rsplit("/", 1)[1]
    if unreal.EditorAssetLibrary.does_asset_exist(struct_path):
        have = signature_of(must(unreal.load_asset(struct_path), "asset " + struct_path))
        if have == want:
            return None, {"unchanged": True}
        raise RuntimeError("changing the fields of an existing struct is not supported yet (%s)" % struct_path)
    tmp = must(core.tools().create_asset("%s_AbfcTmp_%s" % (name, uuid.uuid4().hex[:8]), core.CFG.dest, unreal.UserDefinedStruct, unreal.StructureFactory()), "throwaway struct")
    ed = must(unreal.find_object(tmp, "UserDefinedStructEditorData_0"), "struct editor data")
    off = descriptions_offset()
    new = carrier(lines)
    cn = struct.unpack_from("<Qii", core.rd(core.addr(new) + off, 16))[1]
    if cn != len(lines):
        raise RuntimeError("carrier has %d of %d members" % (cn, len(lines)))
    core.swap(core.addr(ed) + off, core.addr(new) + off, 16)
    dup = must(unreal.EditorAssetLibrary.duplicate_loaded_asset(tmp, struct_path), "duplicate of the throwaway struct")
    got = signature_of(dup)
    if got != want:
        raise RuntimeError("struct members not as requested for %s: wanted %s, got %s" % (struct_path, [n for n, _ in want], [n for n, _ in got]))
    return dup, {"unchanged": False, "members": [n for n, _ in want], "object_path": pth(dup)}
