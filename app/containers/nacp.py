"""Title metadata read out of a container's Control NCA: name, publisher, icon, version.

The workaround for a titledb that never heard of a title - everything shown about a game is
also inside the game. `control.nacp` holds a name and publisher per language plus the human
display version, and the same RomFS holds one JPEG icon per language.

The NACP is parsed here rather than through `nsz.Fs.Nacp`, whose language table stops one
slot short of BrazilianPortuguese and whose accessors want a File wrapper around the blob.
"""
import logging
import struct
from enum import IntEnum

from nsz.Fs import Nca, Type

from .container import nca_holder, open_container, open_nca

logger = logging.getLogger('main')


class NacpLanguage(IntEnum):
    """NACP language slots, in libnx nacp.h order. The member name is the icon file name."""
    AmericanEnglish = 0
    BritishEnglish = 1
    Japanese = 2
    French = 3
    German = 4
    LatinAmericanSpanish = 5
    Spanish = 6
    Italian = 7
    Dutch = 8
    CanadianFrench = 9
    Portuguese = 10
    Russian = 11
    Korean = 12
    TraditionalChinese = 13
    SimplifiedChinese = 14
    BrazilianPortuguese = 15


DEFAULT_LANGUAGE = NacpLanguage.AmericanEnglish

# The language a titledb locale asks for, as Settings LanguageCode splits them: the regional
# variants are the whole point, so 'en' is not one language and neither is 'es'.
_BY_LANGUAGE = {
    'en': NacpLanguage.AmericanEnglish,
    'ja': NacpLanguage.Japanese,
    'fr': NacpLanguage.French,
    'de': NacpLanguage.German,
    'it': NacpLanguage.Italian,
    'es': NacpLanguage.Spanish,
    'nl': NacpLanguage.Dutch,
    'pt': NacpLanguage.Portuguese,
    'ru': NacpLanguage.Russian,
    'ko': NacpLanguage.Korean,
    'zh': NacpLanguage.SimplifiedChinese,
}

# Regions where 'en' means en-US rather than en-GB, and 'es' means es-419 rather than es-ES.
_AMERICAS = {'US', 'CA', 'MX', 'AR', 'BR', 'CL', 'CO', 'PE'}

_BY_REGION = {
    ('CA', 'fr'): NacpLanguage.CanadianFrench,
    ('BR', 'pt'): NacpLanguage.BrazilianPortuguese,
    ('HK', 'zh'): NacpLanguage.TraditionalChinese,
}


def language_for_locale(region, language):
    """The NACP language slot a titledb region/language pair asks for."""
    region, language = (region or '').upper(), (language or '').lower()
    if (region, language) in _BY_REGION:
        return _BY_REGION[(region, language)]
    if language == 'en' and region not in _AMERICAS:
        return NacpLanguage.BritishEnglish
    if language == 'es' and region in _AMERICAS:
        return NacpLanguage.LatinAmericanSpanish
    return _BY_LANGUAGE.get(language, DEFAULT_LANGUAGE)


# --- NACP ---
_NAME_SIZE = 0x200
_ENTRY_SIZE = 0x300
_DISPLAY_VERSION = (0x3060, 0x10)


def _string(blob, offset, size):
    return blob[offset:offset + size].split(b'\0', 1)[0].decode('utf-8', 'replace') or None


def nacp_title(blob, language):
    """(name, publisher) of one language slot of a control.nacp blob."""
    entry = language * _ENTRY_SIZE
    return _string(blob, entry, _NAME_SIZE), _string(blob, entry + _NAME_SIZE, _ENTRY_SIZE - _NAME_SIZE)


def nacp_display_version(blob):
    """The version string a game shows about itself, e.g. '1.0.1'."""
    return _string(blob, *_DISPLAY_VERSION)


# --- RomFS ---
_ROMFS_HEADER = struct.Struct('<10Q')
_ROMFS_FILE_ENTRY = struct.Struct('<IIQQII')
_CONTROL_NACP = 'control.nacp'


def read_romfs_files(rom):
    """{name: (offset, size)} of the files in a RomFS section, offsets absolute within it.

    A control RomFS is flat - `control.nacp` and the icons sit at the root - so the file meta
    table is walked straight through and the directory tables are never needed.
    """
    section = rom.ivfc.levels[-1].offset
    rom.seek(section)
    header = _ROMFS_HEADER.unpack(rom.read(_ROMFS_HEADER.size))
    meta_offset, meta_size, data_offset = header[7], header[8], header[9]

    rom.seek(section + meta_offset)
    meta = rom.read(meta_size)

    files = {}
    offset = 0
    while offset + _ROMFS_FILE_ENTRY.size <= len(meta):
        *_, file_offset, size, _hash, name_len = _ROMFS_FILE_ENTRY.unpack_from(meta, offset)
        name_at = offset + _ROMFS_FILE_ENTRY.size
        name = meta[name_at:name_at + name_len].decode('utf-8', 'replace')
        files[name] = (section + data_offset + file_offset, size)
        offset = name_at + ((name_len + 3) & ~3)
    return files


# --- Extraction ---
def _icon_name(language):
    return f'icon_{language.name}.dat'


def _pick(available, wanted):
    """The wanted language if the title carries it, else English, else whatever it does carry."""
    if wanted in available:
        return wanted
    if DEFAULT_LANGUAGE in available:
        return DEFAULT_LANGUAGE
    return available[0] if available else None


def _read(rom, offset, size):
    rom.seek(offset)
    return rom.read(size)


def _read_control(rom, wanted):
    """Name, publisher and icon of one Control NCA, in the best language it offers."""
    files = read_romfs_files(rom)
    blob = _read(rom, *files[_CONTROL_NACP])

    titles = {lang: nacp_title(blob, lang) for lang in NacpLanguage}
    # Picked apart: a title can carry a name in a language it ships no icon for.
    name_lang = _pick([lang for lang, (name, _) in titles.items() if name], wanted)
    icon_lang = _pick([lang for lang in NacpLanguage if _icon_name(lang) in files], wanted)

    name, publisher = titles[name_lang] if name_lang is not None else (None, None)
    return {
        'name': name,
        'publisher': publisher,
        'display_version': nacp_display_version(blob),
        'icon': _read(rom, *files[_icon_name(icon_lang)]) if icon_lang is not None else None,
        'language': name_lang,
        'icon_language': icon_lang,
    }


def _ncas(holder):
    """The NCAs a container's own open created - under `meta_only`, just the cnmt ones."""
    return [f for f in holder if isinstance(f, Nca.Nca)]


def _romfs_of(nca):
    return next((fs for fs in nca if fs.fsType == Type.Fs.ROMFS), None)


# A CNMT content entry is typed by its own enum, not the NCA header's `Type.Content`: the two
# are shifted by one (Meta=0, Program=1, Data=2, Control=3), so `Type.Content.CONTROL` matches
# a Data entry here.
_CNMT_CONTROL = 3


def _control_owners(ncas):
    """{control nca id: (app id, version)}, read from every CNMT in the container.

    The only thing that can tell apart the contents of a bundle, and the only place the
    content's own app id appears: an update's Control NCA header names the *base* title.
    """
    owners = {}
    for nca in ncas:
        if nca.header.contentType != Type.Content.META:
            continue
        for section in nca:
            cnmt = section.getCnmt()
            owners.update({e.ncaId: (cnmt.titleId.upper(), cnmt.version)
                           for e in cnmt.contentEntries if e.type == _CNMT_CONTROL})
    return owners


def extract_metadata(filepath, language=DEFAULT_LANGUAGE):
    """Per-content metadata read from a container's Control NCAs.

    One dict per content that has one - a DLC ships no Control NCA and yields nothing.
    `title_id` is what the Control NCA header declares, the base title even for an update, so
    it keys title metadata directly; `app_id` and `version` name the content itself.

    Opened `meta_only`, so only the cnmts are read; the Control NCA each one names is then
    opened on its own and the Program NCAs are never touched, which is most of the cost.
    """
    contents = []
    with open_container(filepath, meta_only=True) as container:
        holder = nca_holder(container)
        for nca_id, owner in _control_owners(_ncas(holder)).items():
            nca = open_nca(holder, nca_id)
            rom = _romfs_of(nca) if nca is not None else None
            if rom is None:
                logger.warning(f'Skipping unusable Control NCA {nca_id} in {filepath}.')
                continue
            content = _read_control(rom, language)
            content['app_id'], content['version'] = owner
            content['title_id'] = nca.header.titleId.upper()
            contents.append(content)
    return contents
