"""Choose the files of a document that the window offers, and describe them."""

from __future__ import annotations

import html
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit

from portolan_registry_qgis.core.format import format_label, human_size
from portolan_registry_qgis.core.hrefs import (
    folder_name,
    glob_root,
    plain_vsicurl_path,
    vsicurl_path,
)
from portolan_registry_qgis.core.i18n import N_, _, count, ngettext, number
from portolan_registry_qgis.core.scope import Everything
from portolan_registry_qgis.core.stac import PREFERRED_FORMATS

if TYPE_CHECKING:
    from portolan_registry_qgis.core.download import Plan
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Asset, Document, Loadable

# The two primary buttons. Each adds the first file of its formats, in order.
VIEW_TIP = N_("Fast. Draws with the publisher's style.")
LOAD_TIP = N_("All features and attributes, for analysis.")
TABLE_TIP = N_("All rows and columns, as a table with no geometry.")
# What each button adds, by format. The tooltip leads with it.
VIEW_KINDS = {
    "pmtiles": N_("Adds the vector tiles (PMTiles). They read only the tiles on screen."),
    "cog": N_("Adds the raster (Cloud Optimized GeoTIFF), read in place over HTTP."),
}
LOAD_KINDS = {
    "parquet": N_("Loads the GeoParquet. DuckDB copies the features in the area of interest."),
    "flatgeobuf": N_("Loads the FlatGeobuf, read in place over HTTP."),
    "geojson": N_("Loads the GeoJSON, read in place over HTTP."),
    "csv": N_("Loads the CSV, read in place over HTTP."),
}
VIEW_FORMATS = ("pmtiles", "cog")
LOAD_FORMATS = ("parquet", "flatgeobuf", "geojson", "csv")


@dataclass(frozen=True)
class ItemFiles:
    """The tree row that stands for every item of a collection."""

    href: str
    title: str
    count: int


class Targets(NamedTuple):
    """The files that the two primary buttons add. None hides a button."""

    view: Loadable | None
    load: Loadable | None


@dataclass(frozen=True)
class ButtonState:
    """Which buttons of the window are enabled."""

    add: bool
    view: bool
    load: bool
    mosaic: bool
    download_selected: bool
    download_all: bool


def only_items(document: Document) -> bool:
    """Return whether every child of ``document`` is an item."""
    return bool(document.children) and all(node.kind == "item" for node in document.children)


def first(loadable: tuple[Loadable, ...], formats: tuple[str, ...]) -> Loadable | None:
    """Return the first file of the first format in ``formats`` that has one."""
    for wanted in formats:
        for candidate in loadable:
            if candidate.format == wanted:
                return candidate
    return None


def item_cog(item: Document) -> Asset | None:
    """Return the first COG data asset of an item."""
    return next((asset for asset in item.data_assets if asset.format == "cog"), None)


def item_file(item: Document) -> Loadable | None:
    """Return the file an item row adds: its first data file, in ``PREFERRED_FORMATS`` order."""
    files = tuple(found for found in item.loadable() if found.asset is not None)
    picked = first(files, PREFERRED_FORMATS)
    return replace(picked, title=item.title) if picked is not None else None


def reads_items(document: Document | None, target: Loadable) -> bool:
    """Return whether "Load full data" reads the GeoParquet files of the items below ``document``.

    It does so for a glob that DuckDB cannot list, because HTTPS has no listing.
    """
    asset = target.asset
    return (
        document is not None
        and bool(document.children)
        and target.format == "parquet"
        and asset is not None
        and asset.read_url is None
    )


def is_readable(target: Loadable, document: Document | None = None) -> bool:
    """Return whether DuckDB can read a GeoParquet target, from its URL or from the items."""
    if target.format != "parquet" or target.asset is None or target.asset.read_url is not None:
        return True
    return reads_items(document, target)


def is_table(item: Loadable) -> bool:
    """Return whether ``item`` is a Parquet file with no geometry column."""
    return item.asset is not None and item.asset.tabular


def format_name(item: Loadable) -> str:
    """Return the format of a file in plain words, such as "GeoParquet"."""
    asset = item.asset
    if asset is None:
        return "PMTiles"
    return format_label(asset.format, asset.type, asset.href, tabular=asset.tabular)


def parquet_name(asset: Asset) -> str:
    """Return the layer name of a GeoParquet file, or of the folder that holds its partitions."""
    if asset.title:
        return asset.title
    if asset.glob is not None:
        return folder_name(glob_root(asset.glob))
    return urlsplit(asset.href).path.rsplit("/", 1)[-1].removesuffix(".parquet")


def pick_targets(loadable: tuple[Loadable, ...], *, duckdb_ok: bool) -> Targets:
    """Return the files that "View on map" and "Load full data" add.

    GeoParquet needs DuckDB. Without it, another format loads in its place.
    A document with GeoParquet alone keeps it, and the button shows how to
    install DuckDB.
    """
    formats = LOAD_FORMATS if duckdb_ok else LOAD_FORMATS[1:]
    load = first(loadable, formats) or first(loadable, ("parquet",))
    return Targets(first(loadable, VIEW_FORMATS), load)


def button_tip(target: Loadable, *, view: bool, document: Document | None = None) -> str:
    """Return the tooltip of a primary button that adds ``target``, a file of ``document``.

    The tooltip says what the button adds, what it is for, and which file it
    reads. A table has no geometry, so it loads whole and says so.
    """
    glob = None if view or target.asset is None else target.asset.glob
    if glob is not None and not is_readable(target, document):
        return _(
            "The catalog splits this data into many files at {glob}. "
            "QGIS cannot list files over HTTPS, and the catalog gives no s3:// or gs:// address "
            "and no items. Add single files from All files instead."
        ).format(glob=glob)
    lead, tip = _lead_and_tip(target, view=view)
    # Translators: {title} names a file, and {format} its format, such as GeoParquet.
    file_line = _("File: {title} ({format})")
    lines = [lead, _(tip), file_line.format(title=target.title, format=format_name(target))]
    if glob is not None and reads_items(document, target):
        lines.append(_("Reads the GeoParquet file of each item in the area."))
    elif glob is not None:
        lines.append(_("Reads every file that matches {glob}.").format(glob=glob))
    return "\n".join(line for line in lines if line)


def _lead_and_tip(target: Loadable, *, view: bool) -> tuple[str, str]:
    """Return what a primary button adds, translated, and the untranslated note on its use."""
    if not view and is_table(target):
        return _("Loads the Parquet table. It has no geometry, so it loads whole."), TABLE_TIP
    kind = (VIEW_KINDS if view else LOAD_KINDS).get(target.format)
    return (_(kind) if kind else ""), (VIEW_TIP if view else LOAD_TIP)


def has_parquet(loadable: tuple[Loadable, ...]) -> bool:
    """Return whether ``loadable`` holds a GeoParquet file, which the scope limits."""
    return any(item.format == "parquet" and not is_table(item) for item in loadable)


def is_on_map(item: Loadable, urls: set[str], sources: list[str]) -> bool:
    """Return whether a project layer reads ``item``.

    ``urls`` holds the file URLs that the layers keep in their properties.
    ``sources`` holds the layer sources. The path form in a source depends
    on the GDAL settings of the session, so both forms count.
    """
    paths = (vsicurl_path(item.href), plain_vsicurl_path(item.href))
    read_url = item.asset.read_url if item.asset is not None else None
    if read_url is not None and read_url in urls:
        return True
    return item.href in urls or any(
        item.href == source or any(path in source for path in paths) for source in sources
    )


def button_state(
    document: Document | None,
    targets: Targets,
    *,
    selected: bool,
    ready: bool,
    busy: bool,
) -> ButtonState:
    """Return which buttons the window enables.

    ``busy`` disables the actions that share the progress bar. The All files
    buttons do not wait for the scope.
    """
    load = targets.load
    return ButtonState(
        add=selected,
        view=targets.view is not None,
        load=load is not None
        and is_readable(load, document)
        and (load.format != "parquet" or ready),
        mosaic=document is not None and only_items(document) and ready and not busy,
        download_selected=selected and not busy,
        download_all=document is not None and not busy and ready,
    )


def files_hint(document: Document | None, rows: int) -> str:
    """Return the note under the file list, or "" when the list needs none.

    ``rows`` is the number of rows in the list.
    """
    if document is None:
        return ""
    if only_items(document):
        return _(
            "To add every file in the area as one layer, use Add as one layer above. "
            "Select files here to add or download them whole."
        )
    if rows:
        return ""
    if document.children:
        return _("This entry has no files of its own. Choose a dataset in the tree.")
    return _("This entry lists no files QGIS can open.")


def parquet_to_clip(document: Document, scope: Scope, queried: str | None = None) -> list[Asset]:
    """Return the GeoParquet files that a download of ``scope`` clips.

    With a map view or an area, each GeoParquet file of the document
    downloads as a GeoPackage of the features in the area. A table has no
    geometry, so it downloads whole. The file at ``queried`` always clips.
    """
    return [
        item.asset
        for item in document.loadable()
        if item.asset is not None
        and item.format == "parquet"
        and item.asset.read_url is not None
        and (
            item.asset.href == queried
            or (not isinstance(scope, Everything) and not item.asset.tabular)
        )
    ]


def split_plan(plan: Plan, clip: list[Asset]) -> Plan:
    """Return ``plan`` without the files in ``clip``, which download clipped instead."""
    clipped = {asset.href for asset in clip}
    return replace(plan, files=[entry for entry in plan.files if entry.url not in clipped])


def plan_summary(plan: Plan, clipped: int = 0) -> str:
    """Return the lines that describe a download, before the user confirms it.

    ``clipped`` is the number of GeoParquet files that download clipped.
    """
    files = len(plan.files)
    lines = [
        ngettext("{n} file, at least {size}.", "{n} files, at least {size}.", files).format(
            n=number(files), size=human_size(plan.known_bytes)
        )
    ]
    if clipped:
        lines.append(
            count(
                "{n} GeoParquet file downloads as GeoPackage, with the features in the area only.",
                "{n} GeoParquet files download as GeoPackage, with the features in the area only.",
                clipped,
            )
        )
    if plan.unknown_sizes:
        lines.append(
            count(
                "{n} file does not declare a size.",
                "{n} files do not declare a size.",
                plan.unknown_sizes,
            )
        )
    if plan.failures:
        lines.append(
            count(
                "{n} document could not be read and is left out.",
                "{n} documents could not be read and are left out.",
                len(plan.failures),
            )
        )
    if plan.unlisted:
        lines.append(
            count(
                "The files of {n} partitioned dataset could not be listed and are left out.",
                "The files of {n} partitioned datasets could not be listed and are left out.",
                len(plan.unlisted),
            )
        )
    if plan.truncated:
        lines.append(_("The listing stopped at its document limit."))
    if plan.outside:
        lines.append(
            count(
                "{n} item outside the chosen area is left out.",
                "{n} items outside the chosen area are left out.",
                plan.outside,
            )
        )
    return "\n".join(lines)


def loadable_tip(item: Loadable, *, on_map: bool = False) -> str:
    """Return the tooltip of a file row.

    ``on_map`` adds the note that a layer in the project reads the file.
    """
    lines = [html.escape(item.href)]
    if item.layers:
        lines.append(_("Layers: {layers}").format(layers=html.escape(", ".join(item.layers))))
    if item.asset is not None and item.asset.roles:
        lines.append(_("Roles: {roles}").format(roles=html.escape(", ".join(item.asset.roles))))
    if on_map:
        lines.append(_("A layer in the project already reads this file."))
    return "<br>".join(lines)
