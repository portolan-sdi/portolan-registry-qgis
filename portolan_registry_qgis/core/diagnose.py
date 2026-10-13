"""Explain why QGIS could not open an asset.

QGIS reports a failed GDAL open as ``Cannot open GDAL dataset <uri>:`` followed
by GDAL's own message. That message says what is wrong, so the panel shows it
instead of a bare failure.

QGIS writes that prefix in English in every language. It translates the line
"Provider is not valid", so the caller passes that line's translated start.
"""

from __future__ import annotations

import re

from portolan_registry_qgis.core.i18n import _

# The URI holds colons of its own, so it ends at the first space.
_PREFIX = re.compile(r"^Cannot open GDAL dataset \S+:\s*")  # i18n: skip - QGIS's text
_PROVIDER_INVALID = re.compile(r"^Provider is not valid\b")  # i18n: skip - QGIS's text
# GDAL words a codec it was built without in either of these two ways.
_MISSING_CODEC = re.compile(
    r"missing codec (\w+)|(\w+) compression support is not configured",  # i18n: skip - GDAL's text
    re.IGNORECASE,
)


def gdal_reason(detail: str, invalid: str = "") -> str:
    """Return GDAL's message from a QGIS layer error, or "" when it has none.

    Args:
        detail: The text of the layer's error, as QGIS reports it.
        invalid: The start of QGIS's "Provider is not valid" line in the
            language QGIS runs in. The English line is always dropped.
    """
    lines = (_PREFIX.sub("", raw.strip()) for raw in detail.splitlines())
    return " ".join(line for line in lines if _says_more(line, invalid))


def _says_more(line: str, invalid: str) -> bool:
    """Return whether ``line`` holds more than QGIS's "Provider is not valid" line."""
    if not line or _PROVIDER_INVALID.match(line):
        return False
    return not (invalid and line.startswith(invalid))


def open_failure(href: str, detail: str, *, flatpak: bool, invalid: str = "") -> str:
    """Return a message that says why ``href`` did not open and what to do.

    Args:
        href: The asset's URL.
        detail: The text of the layer's error, as QGIS reports it.
        flatpak: Whether QGIS runs as a Flatpak.
        invalid: The start of QGIS's translated "Provider is not valid" line.
    """
    reason = gdal_reason(detail, invalid)
    codec = _MISSING_CODEC.search(reason)
    if codec is None:
        if reason:
            # Translators: {reason} is GDAL's error message, in English.
            return _("QGIS could not open {href}: {reason}").format(href=href, reason=reason)
        return _("QGIS could not open {href}").format(href=href)
    name = (codec.group(1) or codec.group(2)).upper()
    # Translators: {name} is a compression method, such as ZSTD or LERC.
    message = _(
        "QGIS could not open {href}. The file uses {name} compression, "
        "and the GDAL in this QGIS cannot decode {name}."
    ).format(href=href, name=name)
    if flatpak:
        advice = _(
            "The QGIS Flatpak builds GDAL without {name}. Open the file in a "
            "QGIS from your Linux distribution, conda-forge, or the QGIS installers."
        ).format(name=name)
    else:
        advice = _("Open it in a QGIS whose GDAL includes {name}.").format(name=name)
    return f"{message} {advice}"
