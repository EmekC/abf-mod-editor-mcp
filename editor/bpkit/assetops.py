"""Editor-side asset operations of the bp_asset_ops tool: rename (with referencer fix-up), delete (refused while something
else references the asset) and reparent (same family only). The host validated the paths (specs.check_asset_ops) and
snapshots the stage first; this module only runs on the staged copy and stops at the first failing op, so the host can
restore the snapshot. `recreate` never reaches this module (the host deletes the asset's files).

Deleting and reparenting are the only places in the kit that do so; tests/test_unit.py SafetyTests names this file.
"""
import unreal

from . import core

FAMILIES = (("actor", "Actor"), ("component", "ActorComponent"), ("widget", "UserWidget"))


def _object_path(package):
    return "%s.%s" % (package, package.rsplit("/", 1)[-1])


def _exists(package):
    return bool(unreal.EditorAssetLibrary.does_asset_exist(_object_path(package)))


def _dep_options():
    return unreal.AssetRegistryDependencyOptions(include_soft_package_references=True, include_hard_package_references=True)


def referencers(package):
    """Package names that reference `package` (hard or soft, per the asset registry), without the package itself."""
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    try:
        found = reg.get_referencers(package, _dep_options()) or []
    except Exception as e:
        raise RuntimeError("could not read the referencers of %s: %s" % (package, e))
    return sorted({str(r) for r in found if str(r) != package})


def _is_dirty(asset):
    """True/False when the asset's package dirty flag can be read, None when it cannot."""
    for getter in ("get_package", "get_outermost"):
        pkg = getattr(asset, getter, None)
        if callable(pkg):
            try:
                is_dirty = getattr(pkg(), "is_dirty", None)
                if callable(is_dirty):
                    return bool(is_dirty())
            except Exception:
                pass
    return None


def _save_referencers(mod, names, skip):
    """Save every loaded or loadable referencer of this mod that the fix-up dirtied. Returns the package names saved."""
    touched = []
    for name in sorted(set(names)):
        if name in skip or not name.startswith("/%s/" % mod):
            continue
        asset = unreal.load_asset(_object_path(name))
        if asset is None:
            continue
        dirty = _is_dirty(asset)
        if dirty is False:
            continue
        if not unreal.EditorAssetLibrary.save_loaded_asset(asset, True):
            raise RuntimeError("could not save the referencer " + name)
        if dirty:
            touched.append(name)
    return touched


def rename(op, mod):
    old, new = op["from"], op["to"]
    if not _exists(old):
        raise RuntimeError("rename: %s does not exist" % old)
    if _exists(new):
        raise RuntimeError("rename: the target %s already exists" % new)
    before = referencers(old)
    if not unreal.EditorAssetLibrary.rename_asset(_object_path(old), _object_path(new)):
        raise RuntimeError("rename: the editor refused to rename %s to %s" % (old, new))
    if not _exists(new):
        raise RuntimeError("rename: %s is not there after the rename" % new)
    if _exists(old):        # the rename left a redirector: point every referencer at the new asset, then drop the redirector
        redirector = core.must(unreal.load_asset(_object_path(old)), "redirector " + old)
        if redirector.get_class().get_name() != "ObjectRedirector":
            raise RuntimeError("rename: %s still exists after the rename and is not a redirector" % old)
        fix = getattr(core.tools(), "fixup_referencers", None)
        if not callable(fix):
            raise RuntimeError("rename: this editor build has no AssetTools.fixup_referencers; the references to %s were not fixed" % old)
        fix([redirector])
        if _exists(old):    # fixup_referencers deletes a redirector nothing references any more
            unreal.EditorAssetLibrary.delete_asset(_object_path(old))
            if _exists(old):
                raise RuntimeError("rename: the redirector at %s could not be deleted" % old)
    touched = _save_referencers(mod, before + referencers(new), {old, new})
    if not unreal.EditorAssetLibrary.save_loaded_asset(core.must(unreal.load_asset(_object_path(new)), "asset " + new), False):
        raise RuntimeError("rename: could not save " + new)
    return {"detail": "renamed %s -> %s" % (old, new), "touched": touched}


def delete(op, mod, delete_set):
    asset = op["asset"]
    if not _exists(asset):
        raise RuntimeError("delete: %s does not exist" % asset)
    blocking = [r for r in referencers(asset) if r not in delete_set]
    if blocking:
        raise RuntimeError("delete: %s is still referenced by %s" % (asset, ", ".join(blocking[:10]) + (" ..." if len(blocking) > 10 else "")))
    if not unreal.EditorAssetLibrary.delete_asset(_object_path(asset)):
        raise RuntimeError("delete: the editor could not delete " + asset)
    if _exists(asset):
        raise RuntimeError("delete: %s is still there after the delete" % asset)
    return {"detail": "deleted " + asset, "touched": []}


def _class_path(cls):
    return str(cls.get_path_name())


def _is_child(cls, base_name):
    """True when `cls` derives from unreal.<base_name>. Raises when the editor cannot tell."""
    base = getattr(unreal, base_name, None)
    if base is None:
        raise RuntimeError("unreal.%s is not available, cannot tell the class family" % base_name)
    check = getattr(getattr(unreal, "MathLibrary", None), "class_is_child_of", None)
    if callable(check):
        return bool(check(cls, base.static_class()))
    raise RuntimeError("this editor build cannot answer 'class is child of', cannot tell the class family")


def family(cls):
    for name, base in FAMILIES:
        if _is_child(cls, base):
            return name
    return "object"


def reparent(op, mod):
    asset, parent = op["asset"], op["parent"]
    if not _exists(asset):
        raise RuntimeError("reparent: %s does not exist" % asset)
    bp = core.must(unreal.load_asset(_object_path(asset)), "asset " + asset)
    if bp.get_class().get_name() not in ("Blueprint", "WidgetBlueprint"):
        raise RuntimeError("reparent: %s is not a Blueprint" % asset)
    new_cls = unreal.load_class(None, parent)
    if new_cls is None:
        raise RuntimeError("reparent: class %s cannot be loaded" % parent)
    old_cls = core.must(bp.get_editor_property("parent_class"), "parent class of " + asset)
    if _class_path(old_cls) == _class_path(new_cls):
        return {"detail": "%s already derives from %s" % (asset, parent), "touched": []}
    old_family, new_family = family(old_cls), family(new_cls)
    if old_family != new_family:
        raise RuntimeError("reparent: family mismatch, %s is a %s Blueprint and %s is a %s class (recreate the asset instead)"
                           % (asset, old_family, parent, new_family))
    unreal.BlueprintEditorLibrary.reparent_blueprint(bp, new_cls)
    unreal.BlueprintEditorLibrary.compile_blueprint(bp)
    now = bp.get_editor_property("parent_class")
    if now is None or _class_path(now) != _class_path(new_cls):
        raise RuntimeError("reparent: %s still derives from %s after the reparent" % (asset, _class_path(now) if now is not None else None))
    if not unreal.EditorAssetLibrary.save_loaded_asset(bp, False):
        raise RuntimeError("reparent: could not save " + asset)
    return {"detail": "reparented %s: %s -> %s" % (asset, _class_path(old_cls), parent), "touched": []}


def run(job):
    """Execute job["ops"] in order, stopping at the first failure. Returns {"ok", "results": [{op, ok, detail}], "touched",
    "error"}; it returns instead of raising so the host reads a complete report."""
    mod = job["mod"]
    ops = list(job["ops"])
    delete_set = set(job.get("delete_set") or [o["asset"] for o in ops if o.get("op") == "delete"])
    results, touched, error = [], [], None
    for op in ops:
        kind = op.get("op")
        try:
            if kind == "rename":
                res = rename(op, mod)
            elif kind == "delete":
                res = delete(op, mod, delete_set)
            elif kind == "reparent":
                res = reparent(op, mod)
            else:
                raise RuntimeError("unknown asset op %r (recreate is handled by the host)" % (kind,))
        except Exception as e:
            error = "%s: %s" % (kind, e)
            results.append({"op": kind, "ok": False, "detail": str(e)})
            break
        results.append({"op": kind, "ok": True, "detail": res["detail"]})
        touched.extend(t for t in res["touched"] if t not in touched)
        unreal.SystemLibrary.collect_garbage()
    return {"ok": error is None, "results": results, "touched": touched, "error": error}
