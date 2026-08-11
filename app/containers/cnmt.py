"""CNMT metadata extraction — the title id, type and version a container declares."""
import logging
import re

from nsz.Fs import Nca, Nsp, Pfs0, Type, Xci

from constants import APP_TYPE_MAP

from .container import open_container

logger = logging.getLogger('main')


def get_cnmts(container):
    cnmts = []
    if isinstance(container, Nsp.Nsp):
        try:
            cnmt = container.cnmt()
            cnmts.append(cnmt)
        except Exception as e:
            logger.warning(f'CNMT section not found in Nsp: {e}')
            raise

    elif isinstance(container, Xci.Xci):
        container = container.hfs0['secure']
        for nspf in container:
            if isinstance(nspf, Nca.Nca) and nspf.header.contentType == Type.Content.META:
                cnmts.append(nspf)
        if not cnmts:
            raise ValueError("No META NCA found in XCI secure partition.")

    else:
        raise ValueError(f"Unsupported container type: {type(container).__name__}.")

    return cnmts


def extract_meta_from_cnmt(cnmt_sections):
    contents = []
    for section in cnmt_sections:
        if isinstance(section, Pfs0.Pfs0):
            Cnmt = section.getCnmt()
            titleType = APP_TYPE_MAP[Cnmt.titleType]
            titleId = Cnmt.titleId.upper()
            version = Cnmt.version
            contents.append((titleType, titleId, version))
    if not contents:
        raise ValueError("No Pfs0 sections found in CNMT container.")
    return contents


def identify_file_from_cnmt(filepath):
    contents = []
    try:
        with open_container(filepath, meta_only=True) as container:
            for cnmt_sections in get_cnmts(container):
                contents += extract_meta_from_cnmt(cnmt_sections)
    except OSError as e:
        # Check if the error is due to a missing master_key
        match = re.search(r"master_key_([0-9a-fA-F]{2}) missing from", str(e))
        if match:
            key_index = match.group(1)
            raise ValueError(f"Missing valid master_key_{key_index} from keys file.") from e
        else:
            raise # Re-raise other OSErrors

    return contents
