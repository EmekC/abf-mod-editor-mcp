"""Gameplay tag lists of a mod: Config/Tags/<Mod>Tags.ini.

The Mod Editor and the game read mod tags from  [/Script/GameplayTags.GameplayTagsList]  lines of the form
    GameplayTagList=(Tag="ClassManager.EntityDirector.Name",DevComment="")
This module reads those lines and appends missing tags while keeping the file's newline style. Pure file logic.
"""
import re
from pathlib import Path

TAG_RX = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"
MAX_TAG_LENGTH = 200
SECTION = "[/Script/GameplayTags.GameplayTagsList]"

_TAG_RE = re.compile(TAG_RX)
_LINE_RE = re.compile(r'^\s*GameplayTagList\s*=\s*\(\s*Tag\s*=\s*"([^"]*)"', re.IGNORECASE)


def check_tag(tag):
    """Raise ValueError unless `tag` is a dotted gameplay tag name this module may write."""
    if not isinstance(tag, str) or not tag:
        raise ValueError("gameplay tag must be a non-empty string")
    if len(tag) > MAX_TAG_LENGTH:
        raise ValueError("gameplay tag %r is longer than %d characters" % (tag[:40] + "...", MAX_TAG_LENGTH))
    if not _TAG_RE.match(tag):
        raise ValueError("gameplay tag %r may only hold letters, digits, '_' and '-' in dot separated segments" % tag)


def ini_path(base, mod):
    """Where a mod keeps its own tag list, below the mod folder `base`."""
    return Path(base) / "Config" / "Tags" / ("%sTags.ini" % mod)


def _read_text(path):
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ValueError("%s is saved as UTF-16 (byte order mark); re-save it as UTF-8 so the tags can be read and extended safely" % path)
    return data.decode("utf-8")


def read_tags(path):
    """Every tag named by a GameplayTagList line of the file, in file order. A missing file holds none."""
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in _read_text(p).splitlines():
        m = _LINE_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def ensure_tags(path, tags):
    """Append the tags the file does not list yet (case-insensitive compare) and return the added ones in order.

    Creates the folder and the file (with the section header) when missing. Keeps CRLF when the file uses it and the
    trailing blank line (or its absence) the file had. A bad tag raises ValueError before anything is written."""
    tags = list(tags)
    for t in tags:
        check_tag(t)
    p = Path(path)
    existing = {t.lower() for t in read_tags(p)}
    added = []
    for t in tags:
        if t.lower() not in existing:
            existing.add(t.lower())
            added.append(t)
    if not added:
        return []
    new_lines = ['GameplayTagList=(Tag="%s",DevComment="")' % t for t in added]
    if p.is_file():
        text = _read_text(p)
        nl = "\r\n" if "\r\n" in text else "\n"
        body = text.rstrip("\r\n")
        trail = text[len(body):]
        lines = body.splitlines()
    else:
        nl, trail, lines = "\r\n", "\r\n", []
    head = next((i for i, ln in enumerate(lines) if ln.strip("﻿ \t").lower() == SECTION.lower()), None)
    if head is None:
        lines.append(SECTION)
        head = len(lines) - 1
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break
    at = head + 1
    for i in range(head + 1, end):
        if lines[i].strip():
            at = i + 1
    lines[at:at] = new_lines
    out = nl.join(lines) + (trail if trail else "")
    if not p.parent.is_dir():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(out.encode("utf-8"))
    return added
