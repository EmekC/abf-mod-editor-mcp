"""Pure helpers of the catalog op (no `unreal` import, so the host and the unit tests can use them).

The catalog is names only: /Game Blueprint classes with their parent, /Script classes, structs, enums and function libraries
(with function names). Nothing here loads an asset.
"""
import re

INTERFACE_FLAG = 0x4000      # UClass::ClassFlags CLASS_Interface
INTERFACE_PARENT = "/Script/CoreUObject.Interface"
ERRORS_MAX = 200

_QUOTED = re.compile(r"'([^']+)'")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def tag_class_path(text):
    """Asset registry tag value -> class path. "/Script/CoreUObject.Class'/Script/UMG.UserWidget'" -> "/Script/UMG.UserWidget";
    "" and "None" -> None; a value without quotes is returned as it is."""
    if text is None:
        return None
    s = str(text).strip()
    if s in ("", "None"):
        return None
    m = _QUOTED.search(s)
    return m.group(1) if m else s


def game_entry(package_name, asset_name, asset_class, parent_tag, flags_tag):
    """One /Game *GeneratedClass asset -> {"path", "kind", "parent", "interface"}. Unparsable class flags count as not an interface."""
    try:
        flags = int(str(flags_tag).strip() or 0)
    except (TypeError, ValueError):
        flags = 0
    return {"path": "%s.%s" % (package_name, asset_name), "kind": str(asset_class),
            "parent": tag_class_path(parent_tag), "interface": bool(flags & INTERFACE_FLAG)}


def nearest_parent(path, ancestors):
    """Nearest ancestor path of `path` from a nearest-first ancestor list that may start with `path` itself. None when there is none."""
    items = [a for a in (ancestors or [])]
    if items and items[0] == path:
        items = items[1:]
    return items[0] if items else None


def script_entry(path, parent, is_library, functions):
    """One /Script class -> {"path", "parent"[, "interface": True][, "library": True, "functions": [sorted unique plain names]]}."""
    e = {"path": path, "parent": parent}
    if parent == INTERFACE_PARENT:
        e["interface"] = True
    if is_library:
        e["library"] = True
        e["functions"] = sorted({f for f in (functions or []) if isinstance(f, str) and _IDENT.match(f)})
    return e


def assemble(engine, game, script_classes, structs, enums, errors):
    """The catalog.json document (version 1): lists sorted by path, counts, at most ERRORS_MAX error lines."""
    game = sorted(game, key=lambda e: e["path"])
    script_classes = sorted(script_classes, key=lambda e: e["path"])
    libs = [c for c in script_classes if c.get("library")]
    return {
        "version": 1,
        "engineVersion": engine,
        "game": game,
        "scriptClasses": script_classes,
        "scriptStructs": sorted(structs),
        "scriptEnums": sorted(enums),
        "counts": {"game": len(game), "scriptClasses": len(script_classes), "scriptStructs": len(structs),
                   "scriptEnums": len(enums), "libraries": len(libs),
                   "functions": sum(len(c.get("functions") or []) for c in libs)},
        "errors": list(errors)[:ERRORS_MAX],
    }
