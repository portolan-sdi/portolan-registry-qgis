"""Explain why QGIS could not open an asset.

The panel shows GDAL's own message from the QGIS layer error.
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

    ``invalid`` is the translated start of QGIS's "Provider is not valid"
    line. The English line is always dropped.
    """
    lines = (_PREFIX.sub("", raw.strip()) for raw in detail.splitlines())
    return " ".join(line for line in lines if _says_more(line, invalid))


def _says_more(line: str, invalid: str) -> bool:
    """Return whether ``line`` holds more than QGIS's "Provider is not valid" line."""
    if not line or _PROVIDER_INVALID.match(line):
        return False
    return not (invalid and line.startswith(invalid))


def open_failure(href: str, detail: str, *, flatpak: bool, invalid: str = "") -> str:
    """Return a message that says why ``href`` did not open and what to do."""
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
