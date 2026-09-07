"""Tests for the metadata ownfoil reads out of the files themselves.

No sample game files exist in the repo, so the two format readers are driven with blobs
built here: a NACP is a fixed-stride table of name/publisher pairs and a RomFS is a header
plus a file table, both small enough to construct exactly. What is asserted is what a
caller gets back - which language a title ends up shown in, and which files were found -
never how the bytes were walked.
"""
import struct

import pytest

import media
from containers.container import partition_entries
from containers.nacp import (DEFAULT_LANGUAGE, NacpLanguage, language_for_locale,
                             nacp_display_version, nacp_title, read_romfs_files)

# --- language selection ---

# (locale, the language slot it asks the file for)
LOCALE_CASES = [
    ("US.en", NacpLanguage.AmericanEnglish),
    ("CA.en", NacpLanguage.AmericanEnglish),
    ("GB.en", NacpLanguage.BritishEnglish),
    ("AU.en", NacpLanguage.BritishEnglish),   # en outside the Americas is en-GB
    ("IE.en", NacpLanguage.BritishEnglish),
    ("JP.ja", NacpLanguage.Japanese),
    ("FR.fr", NacpLanguage.French),
    ("CA.fr", NacpLanguage.CanadianFrench),   # region splits fr from fr-CA
    ("DE.de", NacpLanguage.German),
    ("IT.it", NacpLanguage.Italian),
    ("NL.nl", NacpLanguage.Dutch),
    ("RU.ru", NacpLanguage.Russian),
    ("KR.ko", NacpLanguage.Korean),
    ("ES.es", NacpLanguage.Spanish),
    ("MX.es", NacpLanguage.LatinAmericanSpanish),   # es inside the Americas is es-419
    ("AR.es", NacpLanguage.LatinAmericanSpanish),
    ("PT.pt", NacpLanguage.Portuguese),
    ("BR.pt", NacpLanguage.BrazilianPortuguese),
    ("CN.zh", NacpLanguage.SimplifiedChinese),
    ("HK.zh", NacpLanguage.TraditionalChinese),
    ("XX.qq", DEFAULT_LANGUAGE),              # nothing known about it: English
    ("US.EN", NacpLanguage.AmericanEnglish),  # settings are not normalised for us
]


@pytest.mark.parametrize("locale,expected", LOCALE_CASES, ids=[c[0] for c in LOCALE_CASES])
def test_locale_maps_to_a_language_slot(locale, expected):
    assert language_for_locale(*locale.split(".")) == expected


def test_every_libnx_language_is_present():
    """The member name is the icon filename, so a missing or misspelled slot loses an icon."""
    assert [lang.name for lang in NacpLanguage] == [
        "AmericanEnglish", "BritishEnglish", "Japanese", "French", "German",
        "LatinAmericanSpanish", "Spanish", "Italian", "Dutch", "CanadianFrench",
        "Portuguese", "Russian", "Korean", "TraditionalChinese", "SimplifiedChinese",
        "BrazilianPortuguese",
    ]


# --- NACP ---
NACP_SIZE = 0x4000
_ENTRY = 0x300


def build_nacp(titles, display_version="1.2.3"):
    """A control.nacp blob carrying {language: (name, publisher)}."""
    blob = bytearray(NACP_SIZE)
    for language, (name, publisher) in titles.items():
        at = int(language) * _ENTRY
        blob[at:at + len(name.encode())] = name.encode()
        blob[at + 0x200:at + 0x200 + len(publisher.encode())] = publisher.encode()
    blob[0x3060:0x3060 + len(display_version)] = display_version.encode()
    return bytes(blob)


def test_nacp_reads_each_language_from_its_own_slot():
    blob = build_nacp({NacpLanguage.Japanese: ("あつまれ", "任天堂"),
                       NacpLanguage.BrazilianPortuguese: ("Jogo", "Editora")})
    assert nacp_title(blob, NacpLanguage.Japanese) == ("あつまれ", "任天堂")
    # The last slot: nsz's own table stops one short of it.
    assert nacp_title(blob, NacpLanguage.BrazilianPortuguese) == ("Jogo", "Editora")
    assert nacp_title(blob, NacpLanguage.German) == (None, None)
    assert nacp_display_version(blob) == "1.2.3"


# --- RomFS ---
def build_romfs(files, level_offset=0x1000):
    """A RomFS section holding {name: content}, laid out as the real thing is."""
    header_size = 0x50
    meta = bytearray()
    data = bytearray()
    for name in files:
        content = files[name]
        encoded = name.encode()
        meta += struct.pack("<IIQQII", 0, 0xFFFFFFFF, len(data), len(content),
                            0xFFFFFFFF, len(encoded))
        meta += encoded.ljust((len(encoded) + 3) & ~3, b"\0")
        data += content

    meta_offset = header_size
    data_offset = meta_offset + len(meta)
    header = struct.pack("<10Q", header_size, 0, 0, 0, 0, 0, 0,
                         meta_offset, len(meta), data_offset)
    return b"\0" * level_offset + header + bytes(meta) + bytes(data)


class FakeRom:
    """The seek/read surface `read_romfs_files` uses, over a RomFS section blob."""

    def __init__(self, blob, level_offset):
        self.blob = blob
        self.pos = 0
        self.ivfc = type("Ivfc", (), {"levels": [type("L", (), {"offset": level_offset})()]})()

    def seek(self, pos):
        self.pos = pos

    def read(self, size):
        chunk = self.blob[self.pos:self.pos + size]
        self.pos += size
        return chunk


def test_romfs_files_are_found_at_their_own_offsets():
    contents = {"control.nacp": b"N" * 40, "icon_AmericanEnglish.dat": b"\xff\xd8\xffJPEG",
                "icon_Japanese.dat": b"\xff\xd8\xffJP"}
    rom = FakeRom(build_romfs(contents, level_offset=0x1000), level_offset=0x1000)

    files = read_romfs_files(rom)

    assert set(files) == set(contents)
    for name, expected in contents.items():
        offset, size = files[name]
        rom.seek(offset)
        assert rom.read(size) == expected


def test_romfs_of_an_empty_section_is_empty():
    rom = FakeRom(build_romfs({}), level_offset=0x1000)
    assert read_romfs_files(rom) == {}


# --- partition table ---
ENTRY_SIZE = {b"PFS0": 0x18, b"HFS0": 0x40}


def build_partition_table(magic, files):
    """A PFS0 or HFS0 holding {name: content}, laid out as the real thing is."""
    entry_size = ENTRY_SIZE[magic]
    names = b"\0".join(name.encode() for name in files) + b"\0"

    entries, data, name_offset = b"", b"", 0
    for name, content in files.items():
        entries += struct.pack("<QQI", len(data), len(content), name_offset).ljust(
            entry_size, b"\0")
        name_offset += len(name.encode()) + 1
        data += content

    header = magic + struct.pack("<III", len(files), len(names), 0)
    return header + entries + names + data


class FakeHolder:
    """The seek/read surface `partition_entries` uses, over a whole partition."""

    def __init__(self, blob):
        self.blob = blob
        self.pos = 0

    def seek(self, pos):
        self.pos = pos

    def read(self, size):
        chunk = self.blob[self.pos:self.pos + size]
        self.pos += size
        return chunk

    def _readInt(self, size):
        return int.from_bytes(self.read(size), "little")

    def readInt32(self):
        return self._readInt(4)

    def readInt64(self):
        return self._readInt(8)


# NSP files are PFS0; an XCI's secure partition is HFS0, whose entries are 0x40 not 0x18.
@pytest.mark.parametrize("magic", [b"PFS0", b"HFS0"], ids=["pfs0", "hfs0"])
def test_partition_entries_locate_every_file(magic):
    contents = {"aaaa.cnmt.nca": b"META", "bbbb.nca": b"CONTROL" * 3, "cccc.nca": b"PROGRAM"}
    holder = FakeHolder(build_partition_table(magic, contents))

    entries = partition_entries(holder)

    assert set(entries) == set(contents)
    for name, expected in contents.items():
        offset, size = entries[name]
        holder.seek(offset)
        assert holder.read(size) == expected


# --- language fallback, over a whole control section ---

JA, EN, GB, FR = (NacpLanguage.Japanese, NacpLanguage.AmericanEnglish,
                  NacpLanguage.BritishEnglish, NacpLanguage.French)

# (case, languages the title names, languages it ships an icon for, language asked for,
#  name shown, icon served)
FALLBACK_CASES = [
    ("the asked-for language is there", [EN, FR], [EN, FR], FR, FR, FR),
    ("it is not, so English stands in", [EN, FR], [EN, FR], GB, EN, EN),
    # A Japan-only title really does ship no English slot at all.
    ("neither is, so whatever it has", [JA], [JA], GB, JA, JA),
    # Names and icons are picked apart: a title can name a language it draws no icon for.
    ("named in one language, drawn in another", [EN, FR], [JA], FR, FR, JA),
    ("no icon at all", [EN], [], EN, EN, None),
]


@pytest.mark.parametrize("case,named,drawn,wanted,name_lang,icon_lang", FALLBACK_CASES,
                         ids=[c[0] for c in FALLBACK_CASES])
def test_language_falls_back_to_what_the_title_actually_carries(
        case, named, drawn, wanted, name_lang, icon_lang):
    from containers.nacp import _read_control

    files = {"control.nacp": build_nacp({lang: (f"Name {lang.name}", f"Pub {lang.name}")
                                         for lang in named})}
    files.update({f"icon_{lang.name}.dat": f"icon {lang.name}".encode() for lang in drawn})
    rom = FakeRom(build_romfs(files, level_offset=0x800), level_offset=0x800)

    content = _read_control(rom, wanted)

    assert content["name"] == f"Name {name_lang.name}"
    assert content["publisher"] == f"Pub {name_lang.name}"
    assert content["language"] == name_lang
    assert content["icon_language"] == icon_lang
    # `is not None`, not truthiness: AmericanEnglish is slot 0 and so is falsy.
    assert content["icon"] == (f"icon {icon_lang.name}".encode() if icon_lang is not None else None)


# --- icon storage ---
def test_saved_icon_is_readable_and_its_url_tracks_the_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "ICONS_DIR", str(tmp_path / "icons"))
    title_id = "0100000000010000"

    first = media.save_icon(title_id, b"\xff\xd8\xffONE")
    same = media.save_icon(title_id, b"\xff\xd8\xffONE")
    changed = media.save_icon(title_id, b"\xff\xd8\xffTWO")

    assert (tmp_path / "icons" / f"{title_id}.jpg").read_bytes() == b"\xff\xd8\xffTWO"
    assert first == same
    # Same path, new bytes: only the query string can stop a browser serving the old icon.
    assert changed.split("?")[0] == first.split("?")[0]
    assert changed != first


def test_icon_url_points_at_the_route_that_serves_it(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "ICONS_DIR", str(tmp_path / "icons"))
    url = media.save_icon("0100000000010000", b"x")
    assert url.startswith("/api/media/icons/0100000000010000.jpg?")
