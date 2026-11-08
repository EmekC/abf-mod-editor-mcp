"""Editor-side gameplay tags: build a GameplayTag struct from a name, read one back, list the tags the editor knows.

FGameplayTag::TagName is VisibleAnywhere (edit-const), so a plain set_editor_property on the struct may be refused.
make_tag tries the known ways in order and accepts the first whose read-back equals the requested name; if none does it
raises, so a row is never saved with a tag that was not verified.
"""
import re

import unreal

_EXPORT_RX = re.compile(r'TagName\s*=\s*"?([^",)]*)"?')


def _clean(value):
    """Tag name text from a Name/str, "" for an unset tag."""
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text == "None" else text


def tag_name(tag):
    """The tag name held by a GameplayTag struct ("" when unset, None when it cannot be read)."""
    if tag is None:
        return ""
    export = getattr(tag, "export_text", None)
    if callable(export):
        try:
            m = _EXPORT_RX.search(str(export()))
            if m:
                return _clean(m.group(1))
        except Exception:
            pass
    try:
        return _clean(tag.get_editor_property("tag_name"))
    except Exception:
        return None


def _via_import_text(name):
    tag = unreal.GameplayTag()
    importer = getattr(tag, "import_text", None)
    if not callable(importer):
        raise RuntimeError("GameplayTag has no import_text")
    importer('(TagName="%s")' % name)
    return tag


def _via_property(name):
    tag = unreal.GameplayTag()
    tag.set_editor_property("tag_name", unreal.Name(name))
    return tag


STRATEGIES = (("import_text", _via_import_text), ("set_editor_property", _via_property))


def make_tag(name):
    """A GameplayTag struct for `name`, verified by reading it back. Raises RuntimeError when no strategy works."""
    tried = []
    for label, build in STRATEGIES:
        try:
            tag = build(name)
        except Exception as e:
            tried.append("%s: %s" % (label, e))
            continue
        got = tag_name(tag)
        if got == name:
            return tag
        tried.append("%s: read back %r" % (label, got))
    raise RuntimeError("cannot set gameplay tag %r (%s)" % (name, "; ".join(tried)))


def _names_of_list(items, field="tag"):
    out = []
    for it in items or []:
        try:
            n = _clean(it.get_editor_property(field))
        except Exception:
            n = _clean(getattr(it, field, None))
        if n:
            out.append(n)
    return out


def _from_tag_lists():
    out = []
    for obj in unreal.ObjectIterator(unreal.GameplayTagsList):
        out.extend(_names_of_list(obj.get_editor_property("gameplay_tag_list")))
    return out


def _from_table_settings():
    out = []
    settings = unreal.get_default_object(unreal.GameplayTagsSettings)
    for ref in settings.get_editor_property("gameplay_tag_table_list") or []:
        table = ref if hasattr(ref, "get_class") else unreal.EditorAssetLibrary.load_asset(str(ref))
        if table is None:
            continue
        for n in unreal.DataTableFunctionLibrary.get_data_table_column_as_string(table, "Tag") or []:
            n = _clean(n)
            if n:
                out.append(n)
    return out


def _from_manager():
    mgr_cls = getattr(unreal, "GameplayTagsManager", None)
    if mgr_cls is None:
        raise RuntimeError("GameplayTagsManager is not exposed to Python")
    mgr = mgr_cls.get() if hasattr(mgr_cls, "get") else mgr_cls.get_default_object()
    container = unreal.GameplayTagContainer()
    mgr.request_all_gameplay_tags(container, False)
    return _names_of_list(container.get_editor_property("gameplay_tags"), "tag_name")


SOURCES = (("tag_lists", _from_tag_lists), ("table_settings", _from_table_settings), ("manager", _from_manager))


def list_tags():
    """Every gameplay tag the editor knows: {"tags": sorted unique names, "sources": {source: count or "error: ..."}}."""
    found, sources = set(), {}
    for label, read in SOURCES:
        try:
            names = read()
        except Exception as e:
            sources[label] = "error: %s" % e
            continue
        sources[label] = len(names)
        found.update(names)
    return {"tags": sorted(found, key=lambda t: (t.lower(), t)), "sources": sources}
