"""Local format conversion, and a real-word-processor round trip.

Two jobs, both of which Anthropic's sandbox was doing or not doing.

**Converting formats we cannot read.** A legacy binary `.doc` used to be
refused, then converted in Anthropic's container. LibreOffice does the same
conversion as a local subprocess, so the document never leaves the machine.
The associate agent's sandbox remains the fallback where LibreOffice is not
installed (`managed.convert_to_docx`).

**Proving a redline opens.** The honest gap in this project has been that its
output has never been opened by anything other than python-docx, which wrote
it. `pipeline/validate.py` checks the OOXML rules carefully, and that is not
the same as an independent parser accepting the file.

LibreOffice is not Word and is more forgiving of some revision markup, so this
narrows the gap rather than closing it. Someone still has to open one file in
real Word once. But an independent engine that refuses to parse our output is a
real failure, and this is the closest thing that runs in CI.

LibreOffice is MPL-2.0 and invoked as a subprocess, never linked, so no
copyleft obligation reaches this code.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# macOS installs the binary inside the app bundle rather than on PATH.
_CANDIDATES = (
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/opt/libreoffice/program/soffice",
)

TIMEOUT_SECONDS = 120


class ConversionUnavailable(RuntimeError):
    """LibreOffice is not installed, or the conversion failed.

    Always recoverable: every caller has a path that works without it.
    """


def binary() -> str | None:
    """The soffice executable, or None if it is not installed."""
    for candidate in _CANDIDATES:
        found = shutil.which(candidate) if "/" not in candidate else (
            candidate if Path(candidate).exists() else None
        )
        if found:
            return found
    return None


def available() -> bool:
    return binary() is not None


def to_docx(content: bytes, filename: str) -> bytes:
    """Convert a document to .docx locally. Raises if LibreOffice is absent."""
    return _convert(content, filename, "docx")


def _convert(content: bytes, filename: str, target: str) -> bytes:
    executable = binary()
    if not executable:
        raise ConversionUnavailable("LibreOffice is not installed")

    suffix = Path(filename).suffix or ".bin"
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / f"input{suffix}"
        source.write_bytes(content)
        outdir = Path(work) / "out"
        outdir.mkdir()

        try:
            result = subprocess.run(
                [
                    executable,
                    "--headless",
                    "--norestore",
                    # Its own profile, so a conversion never touches or waits on
                    # a user's LibreOffice session.
                    f"-env:UserInstallation=file://{work}/profile",
                    "--convert-to", target,
                    "--outdir", str(outdir),
                    str(source),
                ],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ConversionUnavailable(
                f"conversion timed out after {TIMEOUT_SECONDS}s"
            ) from e

        produced = list(outdir.glob(f"*.{target}"))
        if not produced:
            detail = (result.stderr or b"").decode("utf-8", "replace")[:200]
            raise ConversionUnavailable(f"conversion produced no file: {detail}")
        data = produced[0].read_bytes()

    if not data:
        raise ConversionUnavailable("the converted file was empty")
    log.info("converted %s to .%s locally (%d bytes)", filename, target, len(data))
    return data


def opens_in_libreoffice(content: bytes) -> tuple[bool, str]:
    """Whether an independent word processor can parse this document.

    Used as a CI gate on the redline writer's output. Converting to plain text
    is the cheapest operation that forces a full parse: if the file is
    malformed, LibreOffice fails rather than producing text.
    """
    if not available():
        return True, "skipped: LibreOffice is not installed"
    try:
        _convert(content, "check.docx", "txt")
    except ConversionUnavailable as e:
        return False, str(e)
    return True, "ok"
