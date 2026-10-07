"""What this file actually is, from its bytes rather than its name.

Extensions lie constantly. People rename `.doc` to `.docx` hoping it will work,
mail systems rewrite names, and "Contract.docx" is routinely a PDF someone
exported. Guessing from the extension and then crashing produces the generic
"something went wrong" reply, and a lawyer who gets that once does not send a
second document. That is how the habit dies.

Every format below gets a specific sentence we are willing to email verbatim.
"""

from __future__ import annotations

import logging
import zipfile
from enum import Enum
from io import BytesIO

log = logging.getLogger(__name__)


class Kind(str, Enum):
    DOCX = "docx"
    LEGACY_DOC = "legacy_doc"        # pre-2007 binary Word, or an OLE container
    ENCRYPTED = "encrypted"          # password protected
    PDF = "pdf"
    RTF = "rtf"
    ODT = "odt"                      # OpenDocument text, LibreOffice's own format
    TEXT = "text"
    PPTX = "pptx"
    XLSX = "xlsx"
    ZIP_NOT_DOCX = "zip_not_docx"    # a real zip, but not a Word package
    ARCHIVE_BOMB = "archive_bomb"    # a package that unpacks to far more than it is
    EMPTY = "empty"
    DAMAGED = "damaged"
    UNKNOWN = "unknown"


# What to tell the sender. Written to be read on a phone, and to say what to do
# next rather than what went wrong.
MESSAGES: dict[Kind, str] = {
    Kind.LEGACY_DOC: (
        "That is an older Word format (.doc). Open it in Word, save it as .docx, "
        "and send it back to me. I will review it straight away."
    ),
    Kind.ENCRYPTED: (
        "That document is password protected, so I could not open it. Send me an "
        "unprotected copy and I will review it."
    ),
    Kind.PDF: (
        "That is a PDF. I can review it and put the changes into a Word copy built "
        "from its text. Send me the Word version if you want them in the original "
        "layout."
    ),
    Kind.PPTX: (
        "That is a PowerPoint deck. I can read it and send you notes, but I "
        "cannot put tracked changes into one."
    ),
    Kind.XLSX: (
        "That is an Excel workbook. I can read it and send you notes, but I "
        "cannot put tracked changes into one."
    ),
    Kind.RTF: (
        "That is an RTF file. Save it as .docx and send it back, and I will review it."
    ),
    Kind.ODT: (
        "That is an OpenDocument file (.odt). Save it as .docx and send it back, "
        "and I will review it."
    ),
    Kind.ZIP_NOT_DOCX: (
        "That file has a .docx name but it is not a Word document. It may have been "
        "renamed or damaged in transit. Try sending it again from the original."
    ),
    Kind.ARCHIVE_BOMB: (
        "That file unpacks to far more than any real document does, so I have not "
        "opened it. If it is a genuine document, save a fresh copy from Word and "
        "send that instead."
    ),
    Kind.EMPTY: (
        "That attachment came through empty, which usually means it did not upload "
        "properly. Try attaching it again."
    ),
    Kind.DAMAGED: (
        "That document would not open. It looks like it was damaged in transit, so "
        "try sending it again from the original."
    ),
    Kind.UNKNOWN: (
        "I could not tell what kind of file that is. I can review .docx, .pdf, .txt "
        "and .md documents."
    ),
}


# Bounds on what a zip package (every .docx, .xlsx, .pptx, .odt) may unpack to.
# A 2 MB attachment that inflates to 4 GB of XML takes the container down in
# the middle of someone else's review, and the email that caused it gets
# retried into the same wall. Real contracts sit far inside these: a 300-page
# agreement with embedded images is tens of megabytes unpacked, and ordinary
# XML compresses 5 to 30 times. Python's zipfile stops reading an entry at its
# declared size, so the declared sizes checked here are the ones that bind.
MAX_UNPACKED_BYTES = 250 * 1024 * 1024
MAX_ENTRIES = 5_000
# Ratios above this are only checked once the package is big enough to hurt;
# a tiny file full of one repeated character is harmless.
MAX_RATIO = 200
RATIO_FLOOR_BYTES = 20 * 1024 * 1024


class ArchiveTooLarge(ValueError):
    """The package would unpack to more than any real document does."""


def check_archive(content: bytes | zipfile.ZipFile) -> None:
    """Raise ArchiveTooLarge if a zip package is a decompression bomb.

    Looks only at the central directory, so it costs nothing on a real file.
    Call it before anything inflates a package's entries; `identify` does,
    and every inbound attachment is identified before it is opened.
    """
    package = content if isinstance(content, zipfile.ZipFile) else zipfile.ZipFile(
        BytesIO(content))
    infos = package.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveTooLarge(f"{len(infos)} entries; the limit is {MAX_ENTRIES}")
    unpacked = sum(max(0, i.file_size) for i in infos)
    packed = sum(max(0, i.compress_size) for i in infos) or 1
    if unpacked > MAX_UNPACKED_BYTES:
        raise ArchiveTooLarge(f"unpacks to {unpacked} bytes; the limit is "
                              f"{MAX_UNPACKED_BYTES}")
    if unpacked > RATIO_FLOOR_BYTES and unpacked / packed > MAX_RATIO:
        raise ArchiveTooLarge(f"compression ratio {unpacked / packed:.0f} to 1 over "
                              f"{unpacked} bytes")


def identify(content: bytes, filename: str = "") -> Kind:
    """Classify by content. The filename is only a tie-breaker."""
    if not content:
        return Kind.EMPTY

    if content[:4] == b"%PDF":
        return Kind.PDF
    if content[:5] == b"{\\rtf":
        return Kind.RTF

    # OLE compound file: pre-2007 Word, and also the wrapper Word uses for an
    # encrypted .docx, so the two have to be told apart by their contents.
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        head = content[:8192]
        if b"EncryptedPackage" in head or b"E\x00n\x00c\x00r\x00y\x00p\x00t\x00e\x00d" in head:
            return Kind.ENCRYPTED
        return Kind.LEGACY_DOC

    if content[:2] == b"PK":
        try:
            package = zipfile.ZipFile(BytesIO(content))
            names = set(package.namelist())
        except zipfile.BadZipFile:
            return Kind.DAMAGED
        try:
            check_archive(package)
        except ArchiveTooLarge as e:
            log.warning("refusing %r as a decompression bomb: %s", filename, e)
            return Kind.ARCHIVE_BOMB
        if "word/document.xml" in names:
            return Kind.DOCX
        if "content.xml" in names and "mimetype" in names:
            # LibreOffice's own format. It arrives from firms that run
            # LibreOffice and from lawyers who exported from Google Docs.
            try:
                mimetype = package.read("mimetype")
            except Exception:  # noqa: BLE001 - a damaged entry is not an .odt
                mimetype = b""
            if b"opendocument.text" in mimetype:
                return Kind.ODT
        if any(n.startswith("ppt/") for n in names):
            return Kind.PPTX
        if any(n.startswith("xl/") for n in names):
            return Kind.XLSX
        if "EncryptedPackage" in names:
            return Kind.ENCRYPTED
        return Kind.ZIP_NOT_DOCX

    # Anything that decodes cleanly and has no control bytes is plain text.
    sample = content[:4096]
    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return Kind.TEXT
        except UnicodeDecodeError:
            pass

    if filename.lower().endswith((".docx", ".doc")):
        return Kind.DAMAGED
    return Kind.UNKNOWN


def message(kind: Kind) -> str:
    return MESSAGES.get(kind, MESSAGES[Kind.UNKNOWN])


REVIEWABLE = {Kind.DOCX, Kind.TEXT, Kind.PDF, Kind.PPTX, Kind.XLSX}

# Formats that need converting before they can be read. LibreOffice does this
# locally; the associate agent's sandbox is a fallback for deployments without it.
# With either on offer they are reviewed and redlined like a .docx
# (pipeline/reflow.py); with neither they are refused with the sentence above.
SANDBOX_CONVERTIBLE = {Kind.LEGACY_DOC, Kind.RTF, Kind.ODT}

# Nothing is read in the sandbox any more. Decks and workbooks are read
# in-process with python-pptx and openpyxl, which are the same libraries the
# container has. See docs/sandbox.md for why that matters.
SANDBOX_READABLE: set[Kind] = set()
REDLINEABLE = {Kind.DOCX}
