"""Format values for display."""

from __future__ import annotations

from urllib.parse import urlsplit

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(size: int | None) -> str:
    """Return ``size`` in bytes as a short decimal string, or "" when unknown."""
    if size is None or size < 0:
        return ""
    value = float(size)
    for unit in _UNITS:
        if value < 1000 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
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
    "text/markdown": "Markdown",
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
        return "Parquet"
    if format in _FORMAT_LABELS:
        return _FORMAT_LABELS[format]
    media = (media_type or "").split(";")[0].strip().lower()
    if media in _MEDIA_LABELS:
        return _MEDIA_LABELS[media]
    if media.startswith("image/"):
        return media.removeprefix("image/").removesuffix("+xml").upper()
    name = href.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    if "." in name:
        extension = name.rsplit(".", 1)[-1]
        if 0 < len(extension) <= 8 and extension.isalnum():
            return extension.upper()
    return "File"


def count(number: int, noun: str) -> str:
    """Return ``number noun`` with an English plural, such as "3 collections"."""
    return f"{number:,} {noun}" if number == 1 else f"{number:,} {noun}s"


def catalog_summary(
    collection_count: int | None, total_size: int | None, licenses: tuple[str, ...]
) -> str:
    """Return one line of facts about a catalog, leaving out the unknown ones."""
    parts = []
    if collection_count is not None:
        parts.append(count(collection_count, "collection"))
    size = human_size(total_size)
    if size:
        parts.append(size)
    if 0 < len(licenses) <= 2:
        parts.append(" and ".join(licenses))
    elif licenses:
        parts.append(count(len(licenses), "license"))
    return ", ".join(parts)


# STAC license values that name no license. The details say what they mean.
_LICENSE_WORDS = {
    "other": "Custom terms",
    "proprietary": "Proprietary",
    "various": "Various",
}
# Link titles that only repeat the row name, such as "License".
_GENERIC_TITLES = frozenset({"license", "licence", "terms", "documentation", "link"})


def license_name(value: str | None) -> str:
    """Return a STAC license value in plain words, or "" when there is none."""
    if not value:
        return ""
    return _LICENSE_WORDS.get(value.strip().lower(), value)


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
