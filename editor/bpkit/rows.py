"""Editor-side class-manager row writing (the bp_apply asset-spec key class_manager_row).

A row asset is a ClassManagerModRowAsset: `row_name` (a Name that must equal the asset name; a fresh asset has none),
`override_existing_row` (bool, left at its default) and `row`, a ClassManagerData struct. Only `row.object_class`, the
class reference, and `row.gameplay_tag` when the spec names a tag, are written; the other fields of a fresh
ClassManagerData (release group CORE) are the defaults and stay as they are. These properties are editor-only: dir() does
not list them, but get/set_editor_property work. An existing row keeps everything except the class reference and the
requested tag. Nothing is ever deleted.
"""
import unreal

from . import core, tags
from .core import must

ROW_ASSET_CLASS = "ClassManagerModRowAsset"
ROW_CLASS_FIELD = "object_class"
ROW_TAG_FIELD = "gameplay_tag"


def _same_class(a, b):
    return a is not None and b is not None and a.get_path_name() == b.get_path_name()


def ensure_class_manager_row(asset_path, class_path, gameplay_tag=None):
    """Make the row asset at `asset_path` reference the generated class at `class_path` ("/Mod/Folder/BP_X.BP_X_C"),
    and, when `gameplay_tag` is given, carry that gameplay tag (set only when different, verified by reading it back).
    Creates the row asset when missing. Returns {"asset", "created", "changed", "tag"}; `changed` is true when the asset
    was created or its class reference or tag was different; `tag` is the requested tag name (None when none was
    requested). Saves the asset only when something changed."""
    if asset_path.startswith("/Game/") or asset_path == "/Game" or class_path.startswith("/Game/"):
        raise RuntimeError("refusing class-manager row for /Game paths: %s" % asset_path)
    row_cls = must(getattr(unreal, ROW_ASSET_CLASS, None), "unreal class " + ROW_ASSET_CLASS)
    cls = must(unreal.load_class(None, class_path), "class " + class_path)
    folder, name = asset_path.rsplit("/", 1)
    created = False
    if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
        asset = must(unreal.load_asset(asset_path), "asset " + asset_path)
        if not isinstance(asset, row_cls):
            raise RuntimeError("%s is a %s, not a %s" % (asset_path, asset.get_class().get_name(), ROW_ASSET_CLASS))
    else:
        factory = unreal.DataAssetFactory()
        factory.set_editor_property("data_asset_class", row_cls)
        asset = must(core.tools().create_asset(name, folder, row_cls, factory), "create_asset " + asset_path)
        asset.set_editor_property("row_name", name)
        created = True
    row = asset.get_editor_property("row")
    changed = created or not _same_class(row.get_editor_property(ROW_CLASS_FIELD), cls)
    class_changed = changed
    tag_changed = bool(gameplay_tag) and tags.tag_name(row.get_editor_property(ROW_TAG_FIELD)) != gameplay_tag
    changed = changed or tag_changed
    if changed:
        if class_changed:
            row.set_editor_property(ROW_CLASS_FIELD, cls)
        if tag_changed:
            row.set_editor_property(ROW_TAG_FIELD, tags.make_tag(gameplay_tag))
        asset.set_editor_property("row", row)
        if tag_changed:
            got = tags.tag_name(asset.get_editor_property("row").get_editor_property(ROW_TAG_FIELD))
            if got != gameplay_tag:
                raise RuntimeError("cannot set gameplay tag %r on %s: it reads back as %r" % (gameplay_tag, asset_path, got))
        if not unreal.EditorAssetLibrary.save_loaded_asset(asset, False):
            raise RuntimeError("could not save " + asset_path)
    return {"asset": asset_path, "created": created, "changed": changed, "tag": gameplay_tag or None}
