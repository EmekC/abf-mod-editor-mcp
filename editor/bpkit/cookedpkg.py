"""Read-only reader for the editor's cooked IoStore content (uncompressed, unencrypted): the names a class package spells.

No `unreal` import. Layouts (UE 5.4): .utoc table of contents (header 144 bytes), directory index, compressed-block table, and the
Zen package header (summary, name batch, export map). Only the header of a package is read, never the serialized exports. A format
surprise raises CookedFormatError or yields None; it never produces wrong data silently (callers check every candidate name against
the engine). Nothing here writes.
"""
import os
import struct

TOC_MAGIC = b"-==--==--==--==-"
FLAG_COMPRESSED = 0x01
FLAG_ENCRYPTED = 0x02
FLAG_SIGNED = 0x04
FLAG_INDEXED = 0x08
INVALID = 0xFFFFFFFF
SUMMARY_SIZE = 52          # FZenPackageSummary without versioning info
EXPORT_ENTRY_SIZE = 72
GEN_VARIABLE = "_GEN_VARIABLE"
TIMELINE_TEMPLATE = "_Template"
MIN_TOC_VERSION = 4        # EIoStoreTocVersion::PerfectHash: the section layout parse_toc reads
NOT_INDEXED = "not indexed"
NUMBER_PROBES = 10         # numbered FNames are probed as <base>_0 .. <base>_9


class CookedFormatError(Exception):
    pass


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def parse_toc(data):
    """Header fields and section offsets of a .utoc (bytes). Raises CookedFormatError for a layout this reader does not know."""
    if len(data) < 144 or data[:16] != TOC_MAGIC:
        raise CookedFormatError("not a .utoc file")
    if data[16] < MIN_TOC_VERSION:
        raise CookedFormatError("table of contents version %d is older than the perfect-hash layout (%d)" % (data[16], MIN_TOC_VERSION))
    if _u32(data, 52) != 1:
        raise CookedFormatError("container with %d partitions (only single-partition containers are read)" % _u32(data, 52))
    t = {"data": data, "version": data[16], "header_size": _u32(data, 20), "entry_count": _u32(data, 24), "block_count": _u32(data, 28),
         "block_entry_size": _u32(data, 32), "method_count": _u32(data, 36), "method_length": _u32(data, 40), "block_size": _u32(data, 44),
         "dir_size": _u32(data, 48), "partitions": _u32(data, 52), "flags": data[80], "seeds": _u32(data, 84), "no_hash": _u32(data, 96)}
    if t["header_size"] != 144 or t["block_entry_size"] != 12 or t["block_size"] == 0:
        raise CookedFormatError("unexpected .utoc header (size %d, block entry %d)" % (t["header_size"], t["block_entry_size"]))
    pos = 144
    pos += t["entry_count"] * 12
    t["ol_off"] = pos
    pos += t["entry_count"] * 10
    pos += t["seeds"] * 4 + t["no_hash"] * 4
    t["blocks_off"] = pos
    pos += t["block_count"] * 12
    pos += t["method_count"] * t["method_length"]
    if t["flags"] & FLAG_SIGNED:
        raise CookedFormatError("signed container")
    t["dir_off"] = pos
    if pos + t["dir_size"] > len(data):
        raise CookedFormatError("directory index outside the file")
    return t


def offset_length(toc, entry):
    """(offset, length) of table-of-contents entry `entry` (5-byte big-endian pairs)."""
    o = toc["ol_off"] + entry * 10
    b = toc["data"]
    return int.from_bytes(b[o:o + 5], "big"), int.from_bytes(b[o + 5:o + 10], "big")


def read_range(toc, ucas_path, entry, start, length):
    """`length` bytes at `start` inside the chunk of `entry` (uncompressed blocks only)."""
    if toc["flags"] & FLAG_ENCRYPTED:
        raise CookedFormatError("encrypted container")
    off, size = offset_length(toc, entry)
    if start < 0 or start + length > size:
        raise CookedFormatError("range outside the chunk")
    want = off + start
    end = want + length
    bs = toc["block_size"]
    out = bytearray()
    d = toc["data"]
    with open(ucas_path, "rb") as f:
        pos = want
        while pos < end:
            bi = pos // bs
            if bi >= toc["block_count"]:
                raise CookedFormatError("block %d outside the table" % bi)
            bo = toc["blocks_off"] + bi * 12
            boff = int.from_bytes(d[bo:bo + 5], "little")
            csize = int.from_bytes(d[bo + 5:bo + 8], "little")
            usize = int.from_bytes(d[bo + 8:bo + 11], "little")
            method = d[bo + 11]
            if method != 0:
                raise CookedFormatError("compressed block (method %d)" % method)
            if csize != usize:
                raise CookedFormatError("block sizes differ without compression")
            inner = pos - bi * bs
            take = min(end - pos, usize - inner)
            if take <= 0:
                raise CookedFormatError("empty block read")
            f.seek(boff + inner)
            chunk = f.read(take)
            if len(chunk) != take:
                raise CookedFormatError("short read")
            out += chunk
            pos += take
    return bytes(out)


def _fstring(b, o):
    n = _i32(b, o)
    o += 4
    if n == 0:
        return "", o
    if n > 0:
        return b[o:o + n - 1].decode("latin-1"), o + n
    n = -n
    return b[o:o + 2 * (n - 1)].decode("utf-16-le"), o + 2 * n


def directory_files(toc, project):
    """{"/Game/<rest>": toc entry index} for every `<project>/Content/<rest>.uasset` in the directory index."""
    b = toc["data"][toc["dir_off"]:toc["dir_off"] + toc["dir_size"]]
    _mount, pos = _fstring(b, 0)
    nd = _u32(b, pos)
    pos += 4
    dirs = [struct.unpack_from("<4I", b, pos + i * 16) for i in range(nd)]
    pos += nd * 16
    nf = _u32(b, pos)
    pos += 4
    files = [struct.unpack_from("<3I", b, pos + i * 12) for i in range(nf)]
    pos += nf * 12
    ns = _u32(b, pos)
    pos += 4
    strings = []
    for _ in range(ns):
        s, pos = _fstring(b, pos)
        strings.append(s)
    base = (project + "/Content/").lower()
    out = {}
    stack = [(0, "")] if nd else []
    seen = 0
    while stack:
        di, path = stack.pop()
        seen += 1
        if seen > nd + 5:
            raise CookedFormatError("directory index loops")
        name, first_child, _next, first_file = dirs[di]
        here = path
        if name != INVALID and di != 0:
            here = path + strings[name] + "/"
        fi = first_file
        guard = 0
        while fi != INVALID:
            guard += 1
            if guard > nf + 1:
                raise CookedFormatError("file list loops")
            fname, nxt, user = files[fi]
            full = here + strings[fname]
            if full.lower().startswith(base) and full.endswith(".uasset") and not full.endswith(".o.uasset"):
                out["/Game/" + full[len(base):-len(".uasset")]] = user
            fi = nxt
        ci = first_child
        guard = 0
        while ci != INVALID:
            guard += 1
            if guard > nd + 1:
                raise CookedFormatError("child list loops")
            stack.append((ci, here))
            ci = dirs[ci][2]
    return out


def parse_name_batch(buf, off):
    """(names, end offset) of a serialized FNameEntry batch at `off` (strings are packed, no alignment)."""
    num = _u32(buf, off)
    if num == 0:
        return [], off + 4
    nbytes = _u32(buf, off + 4)
    pos = off + 4 + 4 + 8
    pos += num * 8
    heads = []
    for i in range(num):
        b0, b1 = buf[pos + 2 * i], buf[pos + 2 * i + 1]
        heads.append((bool(b0 & 0x80), ((b0 & 0x7F) << 8) | b1))
    pos += num * 2
    strings_start = pos
    names = []
    for wide, n in heads:
        if wide:
            names.append(buf[pos:pos + 2 * n].decode("utf-16-le"))
            pos += 2 * n
        else:
            names.append(buf[pos:pos + n].decode("latin-1"))
            pos += n
    if pos != strings_start + nbytes:
        raise CookedFormatError("name batch size mismatch (%d vs %d)" % (pos - strings_start, nbytes))
    return names, pos


def mapped_name(names, index, number):
    """FMappedName (name map index with its flag bits, instance number) -> text (`base` or `base_<number-1>`)."""
    i = index & 0x3FFFFFFF
    base = names[i] if i < len(names) else "?"
    return base if not number else "%s_%d" % (base, number - 1)


def _export_index(package_object_index):
    """Export index of an FPackageObjectIndex (type bits 62-63 == 0), else None (null, script import, package import)."""
    if (package_object_index >> 62) != 0:
        return None
    return package_object_index & ((1 << 62) - 1)


def parse_zen_header(buf):
    """Names and export map of a Zen package header (`buf` = the first HeaderSize bytes at least):
    {"header_size", "names": [str], "exports": [{"name", "outer": export index or None}]}."""
    if len(buf) < SUMMARY_SIZE + 4:
        raise CookedFormatError("package header too short")
    if _u32(buf, 0):
        raise CookedFormatError("package header with versioning info")
    header_size = _u32(buf, 4)
    hashes_off, _import_off, export_off, bundle_off = (_i32(buf, 24 + 4 * i) for i in range(4))
    names, end = parse_name_batch(buf, SUMMARY_SIZE)
    if not (end <= hashes_off <= end + 64):
        raise CookedFormatError("name batch ends at %d, expected %d" % (end, hashes_off))
    if export_off < 0 or bundle_off < export_off or bundle_off > len(buf):
        raise CookedFormatError("export map outside the header")
    exports = []
    for i in range((bundle_off - export_off) // EXPORT_ENTRY_SIZE):
        o = export_off + i * EXPORT_ENTRY_SIZE
        exports.append({"name": mapped_name(names, _u32(buf, o + 16), _u32(buf, o + 20)), "outer": _export_index(_u64(buf, o + 24))})
    return {"header_size": header_size, "names": names, "exports": exports}


def numbered_names(names, count=NUMBER_PROBES):
    """`<base>_0` .. `<base>_<count-1>` for every name in `names`. A numbered FName (NewVar_0, Timeline_3) is stored as its base in the
    name map; only an export name spells the number, so plain variables lose it. Names that already end in `_<digits>` are skipped."""
    out = set()
    for n in names:
        stem, _sep, tail = n.rpartition("_")
        if tail.isdigit() and stem:
            continue
        for i in range(count):
            out.add("%s_%d" % (n, i))
    return out


def package_members(header, class_name):
    """Member name candidates of the class `class_name` out of a parsed header, or None when the package has no such class export
    (class names compare case-insensitively, like FNames): {"names": every spelled name (name map, export names, `_GEN_VARIABLE` stems),
    "base": the name-map entries alone (what numbered_names expands), "widgets": exports whose outer is a `WidgetTree` export,
    "components": `<Var>_GEN_VARIABLE` stems, "timelines": `<Var>_Template` stems (timeline templates)}."""
    exports = header["exports"]
    low = class_name.lower()
    if not any(e["name"].lower() == low for e in exports):
        return None
    names = set(header["names"])
    widgets, components, timelines = set(), set(), set()
    for e in exports:
        n = e["name"]
        names.add(n)
        if n.endswith(GEN_VARIABLE) and len(n) > len(GEN_VARIABLE):
            stem = n[:-len(GEN_VARIABLE)]
            components.add(stem)
            names.add(stem)
        elif n.endswith(TIMELINE_TEMPLATE) and len(n) > len(TIMELINE_TEMPLATE):
            stem = n[:-len(TIMELINE_TEMPLATE)]
            timelines.add(stem)
            names.add(stem)
        o = e["outer"]
        if o is not None and o < len(exports) and exports[o]["name"] == "WidgetTree":
            widgets.add(n)
    return {"names": names, "base": set(header["names"]), "widgets": widgets, "components": components, "timelines": timelines}


class CookedIndex:
    """Index of the Indexed, unencrypted, single-partition *.utoc containers in a Paks folder; maps /Game packages to their package
    chunk. A container this reader cannot use (signed, odd version, truncated, several partitions, ...) is skipped and listed in
    `skipped` ([(file name, reason)]) instead of failing the whole index. Package paths compare case-insensitively (asset paths do)."""

    def __init__(self, paks_dir):
        self.paks_dir = paks_dir
        content = os.path.dirname(os.path.abspath(paks_dir))
        self.project = os.path.basename(os.path.dirname(content))
        self.containers = []   # (toc, ucas path)
        self.files = {}        # "/Game/x" -> (container index, toc entry)
        self.skipped = []      # (file name, reason)
        self._lower = {}       # "/game/x" -> (container index, toc entry); first container wins
        for fn in sorted(os.listdir(paks_dir)):
            if not fn.lower().endswith(".utoc"):
                continue
            path = os.path.join(paks_dir, fn)
            ucas = path[:-5] + ".ucas"
            try:
                if not os.path.isfile(ucas):
                    raise CookedFormatError("no matching .ucas file")
                with open(path, "rb") as f:
                    data = f.read()
                if len(data) < 145 or data[:16] != TOC_MAGIC:
                    raise CookedFormatError("not a .utoc file")
                flags = data[80]
                if not (flags & FLAG_INDEXED):
                    self.skipped.append((fn, NOT_INDEXED))
                    continue
                if flags & FLAG_ENCRYPTED:
                    raise CookedFormatError("encrypted container")
                toc = parse_toc(data)
                found = directory_files(toc, self.project)
            except Exception as e:   # a damaged or foreign container must not cost the others (struct.error, IndexError, ...)
                self.skipped.append((fn, (str(e) or type(e).__name__)[:120]))
                continue
            ci = len(self.containers)
            self.containers.append((toc, ucas))
            for pkg, entry in found.items():
                self.files.setdefault(pkg, (ci, entry))
                self._lower.setdefault(pkg.lower(), (ci, entry))
        if not self.containers:
            raise CookedFormatError("no usable container in %s (%s)" % (paks_dir, "; ".join("%s: %s" % s for s in self.skipped) or "none found"))

    def rejected(self):
        """The skipped containers that are worth reporting: everything except plain non-indexed ones (global.utoc)."""
        return [s for s in self.skipped if s[1] != NOT_INDEXED]

    def package_header(self, pkg):
        """Parsed Zen header of a /Game package, or None when the package is unknown."""
        hit = self._lower.get(pkg.lower())
        if hit is None:
            return None
        ci, entry = hit
        toc, ucas = self.containers[ci]
        size = offset_length(toc, entry)[1]
        head = read_range(toc, ucas, entry, 0, min(8, size))
        hs = _u32(head, 4)
        return parse_zen_header(read_range(toc, ucas, entry, 0, min(hs, size)))

    def class_members(self, class_path):
        """package_members of the class `/Game/Dir/Asset.Asset_C`; None when the package or its class export is not found."""
        pkg, _, cname = class_path.partition(".")
        header = self.package_header(pkg)
        if header is None:
            return None
        return package_members(header, cname)

    def class_member_candidates(self, class_path):
        """Every name the class package spells (see package_members["names"]); None when not found."""
        got = self.class_members(class_path)
        return None if got is None else got["names"]
