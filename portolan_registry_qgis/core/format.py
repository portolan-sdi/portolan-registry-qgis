"""Format values for display."""

from __future__ import annotations

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
