"""Format values for display."""

from __future__ import annotations

from urllib.parse import urlsplit

from portolan_registry_qgis.core.i18n import N_, _, count, decimal

# The symbols of the byte units, from bytes to petabytes.
_UNITS = (
    # Translators: The symbol for bytes in a file size. French writes "o", for octet.
    N_("B"),
    # Translators: The symbol for kilobytes in a file size. French writes "ko", for kilooctet.
    N_("KB"),
    # Translators: The symbol for megabytes in a file size. French writes "Mo", for mégaoctet.
    N_("MB"),
    # Translators: The symbol for gigabytes in a file size. French writes "Go", for gigaoctet.
    N_("GB"),
    # Translators: The symbol for terabytes in a file size. French writes "To", for téraoctet.
    N_("TB"),
    # Translators: The symbol for petabytes in a file size. French writes "Po", for pétaoctet.
    N_("PB"),
)


def human_size(size: int | None) -> str:
    """Return ``size`` in bytes as a short decimal string, or "" when unknown."""
    if size is None or size < 0:
        return ""
    value = float(size)
    for unit in _UNITS:
        if value < 1000 or unit == _UNITS[-1]:
            digits = 0 if unit == _UNITS[0] else 1
            return f"{decimal(value, digits)} {_(unit)}"
        value /= 1000
    return ""  # pragma: no cover - the loop always returns


_FORMAT_LABELS = {
    "pmtiles": "PMTiles",
    "parquet": "GeoParquet",
    "cog": "COG",
    "geojson": "GeoJSON",
    "flatgeobuf": "FlatGeobuf",
    "copc": "COPC",
}
_MEDIA_LABELS = {
    "application/json": "JSON",
    "text/html": "HTML",
    "text/markdown": "Markdown",  # i18n: skip - a format name
    "text/csv": "CSV",
    "application/pdf": "PDF",
    "application/zip": "ZIP",
}


def format_label(
    format: str | None, media_type: str | None, href: str, tabular: bool = False
) -> str:
    """Return a short name for an asset's format.

    A detected format names itself. Parquet with no geometry column is
    "Parquet", not "GeoParquet". Otherwise the label comes from the media
    type, then from the file extension, and is "File" when neither helps.
    """
    if format == "parquet" and tabular:
        return "Parquet"  # i18n: skip - a format name
    if format in _FORMAT_LABELS:
        return _FORMAT_LABELS[format]
    # Translators: The format of a file whose format is not known.
    return _media_label(media_type) or _extension_label(href) or _("File")


def _media_label(media_type: str | None) -> str | None:
    """Return the format name a media type gives, or None."""
    media = (media_type or "").split(";")[0].strip().lower()
    if media in _MEDIA_LABELS:
        return _MEDIA_LABELS[media]
    if media.startswith("image/"):
        return media.removeprefix("image/").removesuffix("+xml").upper()
    return None


def _extension_label(href: str) -> str | None:
    """Return the file extension of ``href`` in capitals, or None when it has none."""
    name = href.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    extension = name.rsplit(".", 1)[-1] if "." in name else ""
    return extension.upper() if 0 < len(extension) <= 8 and extension.isalnum() else None


def catalog_summary(
    collection_count: int | None, total_size: int | None, licenses: tuple[str, ...]
) -> str:
    """Return one line of facts about a catalog, leaving out the unknown ones."""
    parts = []
    if collection_count is not None:
        parts.append(count("{n} collection", "{n} collections", collection_count))
    size = human_size(total_size)
    if size:
        parts.append(size)
    if len(licenses) == 1:
        parts.append(licenses[0])
    elif len(licenses) == 2:
        # Translators: Two license names, such as "CC0-1.0 and ODbL-1.0".
        parts.append(_("{first} and {second}").format(first=licenses[0], second=licenses[1]))
    elif licenses:
        parts.append(count("{n} license", "{n} licenses", len(licenses)))
    return ", ".join(parts)


# STAC license values that name no license. The details say what they mean.
_LICENSE_WORDS = {
    "other": N_("Custom terms"),
    "proprietary": N_("Proprietary"),
    "various": N_("Various"),
}
# Link titles that only repeat the row name, such as "License".
_GENERIC_TITLES = frozenset(
    {
        "license",
        "licence",
        "terms",
        "documentation",
        "link",
        "source",
        "original source",  # i18n: skip - compared with a lowercased title
    }
)


def license_name(value: str | None) -> str:
    """Return a STAC license value in plain words, or "" when there is none."""
    if not value:
        return ""
    word = _LICENSE_WORDS.get(value.strip().lower())
    return _(word) if word else value


def link_label(href: str, title: str | None) -> str:
    """Return a short name for a link.

    A title that says more than the row name is used. Otherwise the label is
    the file name of a document such as README.md, or the host of a page.
    """
    if title and title.strip().lower() not in _GENERIC_TITLES:
        return title.strip()
    parts = urlsplit(href)
    name = parts.path.rsplit("/", 1)[-1]
    if "." in name and not parts.fragment:
        return name
    host = parts.hostname or href
    return host.removeprefix("www.")


def excerpt(text: str, limit: int = 240) -> str:
    """Return the first ``limit`` characters of ``text`` on one line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"
