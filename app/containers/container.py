"""nsz container access — the one place that turns a path into an open Nsp/Xci.

Everything that walks an open container lives here; `nacp` is handed the bytes and parses
them. `read_file` is what the pipeline calls: one open serving both readers, because the cnmt
that says what a content *is* also names the Control NCA that says what it is *called*.
"""
import logging
import re
from contextlib import contextmanager
from pathlib import Path

from nsz.Fs import (Nca as _Nca, Nsp as _Nsp, Xci as _Xci, Pfs0 as _Pfs0, Type as _Type,
                    factory as _factory)
from nsz.nut import Print as _nsz_print

from constants import APP_TYPE_MAP

from .nacp import DEFAULT_LANGUAGE, read_control

logger = logging.getLogger('main')

_nsz_print.enableInfo = False
_nsz_print.minimalOutput = True
_Pfs0.Print.silent = True


@contextmanager
def open_container(filepath, meta_only=False):
    """Open an NSP/NSZ/XCI/XCZ for reading, closed however the caller leaves."""
    path = Path(filepath)
    container = _factory(path.with_suffix(path.suffix.lower()))
    if type(container) not in (_Nsp.Nsp, _Xci.Xci):
        raise ValueError(f'Unsupported container extension: {path.suffix}')
    try:
        container.open(str(filepath), 'rb', meta_only=meta_only)
        yield container
    finally:
        container.flush()
        container.close()


def nca_holder(container):
    """The PFS0/HFS0 an opened container keeps its NCAs in."""
    return container.hfs0['secure'] if isinstance(container, _Xci.Xci) else container


# PFS0 and HFS0 lay out a partition table the same way - magic, file count, string table
# size, padding, entries, string table - and differ only in how wide one entry is.
_ENTRY_SIZE = {b'PFS0': 0x18, b'HFS0': 0x40}


def partition_entries(holder):
    """{name: (offset, size)} read straight off a PFS0/HFS0 partition table.

    `Pfs0.open` finishes by opening every partition it created, and `meta_only` is what
    decides which ones it creates - so re-reading the table is how to reach a file it skipped
    without paying for the ones it skipped on purpose.
    """
    holder.seek(0)
    entry_size = _ENTRY_SIZE[holder.read(4)]
    count = holder.readInt32()
    string_table_size = holder.readInt32()
    holder.readInt32()  # padding
    header_size = 0x10 + count * entry_size + string_table_size

    holder.seek(0x10 + count * entry_size)
    string_table = holder.read(string_table_size)

    entries = {}
    for i in range(count):
        holder.seek(0x10 + i * entry_size)
        offset, size, name_offset = holder.readInt64(), holder.readInt64(), holder.readInt32()
        name = string_table[name_offset:].split(b'\0', 1)[0].decode('utf-8', 'replace')
        entries[name] = (header_size + offset, size)
    return entries


def open_nca(holder, nca_id):
    """Open one NCA by id, whether or not the container's own open created it.

    Entries are named `<ncaId>.nca` and only a meta NCA is `<ncaId>.cnmt.nca`, so the id
    prefixes exactly one name. Returns None if the container does not carry it.
    """
    entries = partition_entries(holder)
    name = next((n for n in entries if n.startswith(nca_id)), None)
    if name is None:
        return None
    offset, size = entries[name]
    nca = _Nca.Nca()
    holder.partition(offset, size, nca, autoOpen=False)
    nca.open(None, 'rb')
    return nca


# A CNMT content entry is typed by its own enum, not the NCA header's `Type.Content`: the two
# are shifted by one (Meta=0, Program=1, Data=2, Control=3), so `Type.Content.CONTROL` matches
# a Data entry here.
_CNMT_CONTROL = 3


def read_cnmts(holder):
    """Identification tuples and Control NCA owners, from one walk of every cnmt.

    Both halves come off the same cnmt, which is why identifying a file and reading its own
    metadata cost one pass: the title a cnmt declares identifies the content, and its content
    entries name the Control NCA that describes it. The owner is also the only place a
    content's own app id appears - an update's Control NCA header names the *base* title.
    """
    contents, owners = [], {}
    for nca in holder:
        if not isinstance(nca, _Nca.Nca) or nca.header.contentType != _Type.Content.META:
            continue
        for section in nca:
            if not isinstance(section, _Pfs0.Pfs0):
                continue
            cnmt = section.getCnmt()
            title_id = cnmt.titleId.upper()
            contents.append((APP_TYPE_MAP[cnmt.titleType], title_id, cnmt.version))
            owners.update({e.ncaId: (title_id, cnmt.version)
                           for e in cnmt.contentEntries if e.type == _CNMT_CONTROL})
    if not contents:
        raise ValueError('No CNMT found in container.')
    return contents, owners


def _romfs_of(nca):
    return next((fs for fs in nca if fs.fsType == _Type.Fs.ROMFS), None)


def read_controls(holder, owners, language=DEFAULT_LANGUAGE):
    """Per-content metadata read from the Control NCAs `owners` names.

    One dict per content that has one - a DLC ships no Control NCA and yields nothing.
    `title_id` is what the Control NCA header declares, the base title even for an update, so
    it keys title metadata directly; `app_id` and `version` name the content itself.

    The holder is opened `meta_only`, so the Control NCAs are hand-opened one by one and the
    Program NCAs are never touched, which is most of the cost. One unreadable Control NCA is
    skipped rather than raised: it shares its open with identification, which must survive it.
    """
    contents = []
    for nca_id, owner in owners.items():
        try:
            nca = open_nca(holder, nca_id)
            rom = _romfs_of(nca) if nca is not None else None
            if rom is None:
                logger.warning(f'Skipping unusable Control NCA {nca_id}.')
                continue
            content = read_control(rom, language)
        except Exception as e:
            logger.warning(f'Could not read Control NCA {nca_id}: {e}')
            continue
        content['app_id'], content['version'] = owner
        content['title_id'] = nca.header.titleId.upper()
        contents.append(content)
    return contents


def read_file(filepath, language=DEFAULT_LANGUAGE):
    """(identification tuples, per-content metadata) from a single `meta_only` open.

    Raises if the cnmts cannot be read - there is no identification without them. A Control
    NCA that cannot be read only costs its own metadata.
    """
    try:
        with open_container(filepath, meta_only=True) as container:
            holder = nca_holder(container)
            contents, owners = read_cnmts(holder)
            return contents, read_controls(holder, owners, language)
    except OSError as e:
        # Check if the error is due to a missing master_key
        match = re.search(r"master_key_([0-9a-fA-F]{2}) missing from", str(e))
        if match:
            raise ValueError(f"Missing valid master_key_{match.group(1)} from keys file.") from e
        raise
