"""Tests for the metadata ownfoil reads out of the files themselves.

No sample game files exist in the repo, so the two format readers are driven with blobs
built here: a NACP is a fixed-stride table of name/publisher pairs and a RomFS is a header
plus a file table, both small enough to construct exactly. What is asserted is what a
caller gets back - which language a title ends up shown in, and which files were found -
never how the bytes were walked.
"""
import io
import itertools
import os
import struct
import types

import pytest
from nsz.Fs import Nca, Pfs0, Type
from PIL import Image

from containers.container import partition_entries, read_cnmts
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
    from containers.nacp import read_control

    files = {"control.nacp": build_nacp({lang: (f"Name {lang.name}", f"Pub {lang.name}")
                                         for lang in named})}
    files.update({f"icon_{lang.name}.dat": f"icon {lang.name}".encode() for lang in drawn})
    rom = FakeRom(build_romfs(files, level_offset=0x800), level_offset=0x800)

    content = read_control(rom, wanted)

    assert content["name"] == f"Name {name_lang.name}"
    assert content["publisher"] == f"Pub {name_lang.name}"
    assert content["language"] == name_lang
    assert content["icon_language"] == icon_lang
    # `is not None`, not truthiness: AmericanEnglish is slot 0 and so is falsy.
    assert content["icon"] == (f"icon {icon_lang.name}".encode() if icon_lang is not None else None)


# --- cnmt walk ---
#
# The real classes are subclassed rather than mocked because `read_cnmts` picks what to walk
# with isinstance - which is how it stays indifferent to NSP vs XCI.
CONTROL_ENTRY = 3


class FakeCnmtSection(Pfs0.Pfs0):
    def __init__(self, cnmt):
        super().__init__(None)
        self._cnmt = cnmt

    def getCnmt(self):
        return self._cnmt


class FakeNca(Nca.Nca):
    def __init__(self, content_type, sections=()):
        super().__init__()
        self.header = types.SimpleNamespace(contentType=content_type)
        self._sections = list(sections)

    def __iter__(self):
        return iter(self._sections)


def build_cnmt_nca(title_id, version, title_type, control_ids=()):
    entries = [types.SimpleNamespace(ncaId=i, type=CONTROL_ENTRY) for i in control_ids]
    cnmt = types.SimpleNamespace(titleId=title_id, version=version, titleType=title_type,
                                 contentEntries=entries)
    return FakeNca(Type.Content.META, [FakeCnmtSection(cnmt)])


def test_every_cnmt_is_walked_not_just_the_first():
    """`Nsp.cnmt()` stops at the first one, so a bundle used to identify as a single content."""
    holder = [build_cnmt_nca("0100aaa000000000", 0, 128, ["ctrl-base"]),
              build_cnmt_nca("0100aaa000000800", 65536, 129, ["ctrl-upd"]),
              FakeNca(Type.Content.PROGRAM)]

    contents, owners = read_cnmts(holder)

    assert contents == [("BASE", "0100AAA000000000", 0), ("UPDATE", "0100AAA000000800", 65536)]
    assert owners == {"ctrl-base": ("0100AAA000000000", 0),
                      "ctrl-upd": ("0100AAA000000800", 65536)}


def test_a_content_without_a_control_nca_still_identifies():
    """A DLC ships no Control NCA; it must still be found, just with nothing to extract."""
    contents, owners = read_cnmts([build_cnmt_nca("0100aaa000001000", 0, 130)])

    assert contents == [("DLC", "0100AAA000001000", 0)]
    assert owners == {}


def test_a_container_with_no_cnmt_is_an_error():
    """There is no identification without a cnmt, so the caller has to hear about it."""
    with pytest.raises(ValueError):
        read_cnmts([FakeNca(Type.Content.PROGRAM)])


# --- which file of a title supplies its record ---
#
# A base and its updates all carry a Control NCA naming the same title, and an update can
# change any of what it says - No Man's Sky ships one icon in 5.7.5 and another in 6.24.0.
# The newest update is the answer, whatever order the files were read in.

TITLE_ID = "0100853015E86000"
UPDATE_ID = "0100853015E86800"


def _jpeg(color):
    """An icon-shaped image, one flat colour per variant so the wrong one is visible."""
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _content(app_id, version, display_version, color):
    """One Control NCA's worth of metadata, as `container.read_file` returns it."""
    return {"app_id": app_id, "version": version, "title_id": TITLE_ID,
            "name": f"No Man's Sky {display_version}",
            "publisher": f"Hello Games {display_version}",
            "display_version": display_version, "icon": _jpeg(color)}


BASE = _content(TITLE_ID, 0, "1.0.0", "blue")
OLD_UPDATE = _content(UPDATE_ID, 4063232, "5.7.5", "red")
NEW_UPDATE = _content(UPDATE_ID, 4718592, "6.24.0", "green")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An app with an empty library database and an empty media store."""
    import db as db_mod
    import media
    import settings as settings_mod
    import titledb
    from app import create_app
    from db import db, init_db

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(db_mod, "DB_FILE", str(config / "ownfoil.db"))
    monkeypatch.setattr(titledb.store, "DB_FILE", str(config / "ownfoil.db"))
    # Never created, so the projection into titles.db is a no-op: what is asserted here is
    # the durable override in ownfoil.db, which is what a rebuild projects from.
    monkeypatch.setattr(titledb.store, "TITLES_DB_FILE", str(config / "titles.db"))
    monkeypatch.setattr(media, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(config / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "KEYS_FILE", str(config / "keys.txt"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)

    app = create_app(f"sqlite:///{config / 'ownfoil.db'}")
    with app.app_context():
        init_db(app)
        db.session.add(db_mod.Libraries(path="/library"))
        db.session.commit()
    return app


def _extract(app, *contents):
    """Read one file's worth of metadata, as the pipeline's extract stage does."""
    import tasks as tasks_mod
    from db import Files, db

    with app.app_context():
        first = contents[0] if contents else {"app_id": "dlc", "version": 0}
        name = f"{first['app_id']}[v{first['version']}].nsp"
        file = Files(library_id=1, filepath=f"/library/{name}", filename=name, identified=True)
        db.session.add(file)
        db.session.commit()
        tasks_mod._store_metadata(file, list(contents))


def _stored(app, content):
    """(the title's record, the icon filling its slot) against what `content` would store."""
    import media
    from db import Media, list_title_overrides
    from titledb.schema import SOURCE_EXTRACT

    with app.app_context():
        row = next(r for r in list_title_overrides(SOURCE_EXTRACT) if r["id"] == TITLE_ID)
        record = {k: row[k] for k in ("name", "publisher", "icon_url", "extract_version")}
        # Content-addressed, so re-storing the bytes yields the URL they were filed under.
        expected = {"name": content["name"], "publisher": content["publisher"],
                    "icon_url": media.store_extracted(TITLE_ID, media.ICON, content["icon"]),
                    "extract_version": content["version"]}
        slot = Media.query.filter_by(title_id=TITLE_ID, kind=media.ICON, position=0).one()
        return (record, os.path.basename(row["icon_url"] or "")), (expected, slot.filename)


ORDERS = list(itertools.permutations([BASE, OLD_UPDATE, NEW_UPDATE]))


@pytest.mark.parametrize("order", ORDERS,
                         ids=["-".join(c["display_version"] for c in o) for o in ORDERS])
def test_the_newest_update_supplies_the_record_whatever_order_the_files_are_read_in(
        install, order):
    for content in order:
        _extract(install, content)

    stored, expected = _stored(install, NEW_UPDATE)
    assert stored == expected


@pytest.mark.parametrize("order", ORDERS,
                         ids=["-".join(c["display_version"] for c in o) for o in ORDERS])
def test_a_container_holding_several_contents_picks_the_newest_of_them(install, order):
    """A multi-content NSP is the same contest, decided inside a single file."""
    _extract(install, *order)

    stored, expected = _stored(install, NEW_UPDATE)
    assert stored == expected


def test_a_dlc_stores_nothing(install):
    """A DLC ships no Control NCA, so there is no record - but the file is still done."""
    from db import Files, list_title_overrides
    from titledb.schema import SOURCE_EXTRACT

    _extract(install)

    with install.app_context():
        assert list_title_overrides(SOURCE_EXTRACT) == []
        assert Files.query.one().metadata_extracted
