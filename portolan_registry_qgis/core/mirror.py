"""Find the COG files of a collection from its stac-geoparquet mirror."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from portolan_registry_qgis.core import parquet_query
from portolan_registry_qgis.core import scope as scopes
from portolan_registry_qgis.core.geoparquet import (
    column_path,
    implicit_covering,
    parse_json,
    quote,
    shape,
)
from portolan_registry_qgis.core.hrefs import asset_href
from portolan_registry_qgis.core.i18n import _
from portolan_registry_qgis.core.stac import assets_of, only_data

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from portolan_registry_qgis.core.geoparquet import Covering, ReadPlan
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Asset, Bbox, Document

# The role of a collection asset that holds every item as stac-geoparquet.
MIRROR_ROLE = "collection-mirror"


# GDAL BuildVRT reports a file it cannot open with only this message. GDAL
# writes it in English, so it stays English here.
_SKIP_START = "Can't open "  # i18n: skip - a GDAL message
_SKIP_END = ". Skipping it"  # i18n: skip - a GDAL message


def skipped_path(message: str) -> str | None:
    """Return the path in GDAL's "Can't open <path>. Skipping it" message.

    Returns:
        The path, or None for any other message.
    """
    if message.startswith(_SKIP_START) and message.endswith(_SKIP_END):
        return message[len(_SKIP_START) : -len(_SKIP_END)]
    return None


def mirror_href(collection: Document) -> str | None:
    """Return the href of the collection's stac-geoparquet mirror, if it has one."""
    return next(
        (asset.href for asset in collection.assets if MIRROR_ROLE in asset.roles),
        None,
    )


def item_documents(collection: Document) -> dict[str, str]:
    """Map each item ID to the URL of its document, from the collection's item links.

    A link names no ID, so the ID is the file name of the item document
    without ``.json``. This fits the usual layouts ``<id>/<id>.json`` and
    ``<id>.json``.
    """
    found: dict[str, str] = {}
    for node in collection.children:
        if node.kind != "item":
            continue
        name = urlsplit(node.href).path.rsplit("/", 1)[-1]
        found.setdefault(name.removesuffix(".json"), node.href)
    return found


def cog_href(assets: Iterable[Asset]) -> str | None:
    """Return the href of the first COG data asset, or None when there is none."""
    return next((asset.href for asset in only_data(assets) if asset.format == "cog"), None)


def _covering(plan: ReadPlan) -> Covering | None:
    """Return the struct fields of the ``bbox`` column, or None for a list.

    A plan for a table does not look for a covering, so the function looks
    in the attribute columns too.
    """
    if plan.covering is not None:
        return plan.covering
    return implicit_covering([(column.name, column.duckdb_type) for column in plan.columns])


def _bbox_columns(plan: ReadPlan) -> tuple[str, str, str, str]:
    """Return the SQL that reads xmin, ymin, xmax, and ymax from the bbox column.

    Raises:
        ValueError: The file has no ``bbox`` or ``assets`` column.
    """
    names = {column.name for column in plan.columns}
    covering = _covering(plan)
    if "assets" not in names or (covering is None and "bbox" not in names):
        raise ValueError(_("The file has no bbox or assets column"))
    if covering is not None:
        return (
            column_path(covering.xmin),
            column_path(covering.ymin),
            column_path(covering.xmax),
            column_path(covering.ymax),
        )
    half = "len(bbox) // 2"
    return ("bbox[1]", "bbox[2]", f"bbox[{half} + 1]", f"bbox[{half} + 2]")


def mirror_query(plan: ReadPlan, scope: Scope) -> tuple[str, list[Any]]:
    """Build the DuckDB query for the items of a stac-geoparquet file in ``scope``.

    The file URL is the first ``?``. Rows that cross the antimeridian stay for
    the caller to test.
    """
    xmin, ymin, xmax, ymax = _bbox_columns(plan)
    names = {column.name for column in plan.columns}
    links = "to_json(links)" if "links" in names else "NULL"
    ids = quote("id") if "id" in names else "NULL"
    sql = (
        f"SELECT {xmin}, {ymin}, {xmax}, {ymax}, to_json(assets), {links}, {ids} "  # noqa: S608  # nosec B608 - the SQL holds column names only
        "FROM read_parquet(?)"
    )
    params: list[Any] = []
    box = scopes.bbox_of(scope)
    if box is None or box[0] > box[2]:
        return sql, params
    west, south, east, north = box
    where = [
        f"({xmin} > {xmax} OR ({xmax} >= ? AND {xmin} <= ?))",
        f"{ymax} >= ?",
        f"{ymin} <= ?",
    ]
    params += [west, east, south, north]
    if isinstance(scope, scopes.Area) and plan.has_geometry:
        where.append(f"({xmin} > {xmax} OR ST_Intersects({shape(plan)}, ST_GeomFromText(?)))")
        params.append(scope.wkt)
    return f"{sql} WHERE {' AND '.join(where)}", params


def mirror_hrefs(
    con: Any,
    url: str,
    scope: Scope,
    cancelled: Callable[[], bool] | None = None,
    items: dict[str, str] | None = None,
) -> list[str]:
    """Return the COG hrefs of the items of a stac-geoparquet file in ``scope``.

    A relative href resolves against the item's ``self`` link, else its URL
    in ``items``, else the file.
    """
    plan = parquet_query.read_plan(con, url, cancelled)
    sql, params = mirror_query(plan, scope)
    # The cursor stops the running query when ``cancelled`` returns True.
    with parquet_query.cursor(con, cancelled) as cursor:
        rows = cursor.execute(sql, [url, *params]).fetchall()
    found = (row_href(row, url, scope, items or {}) for row in rows)
    return list(dict.fromkeys(href for href in found if href is not None))


def row_href(row: tuple[Any, ...], url: str, scope: Scope, items: dict[str, str]) -> str | None:
    """Return the COG href of one row of ``mirror_query``, or None to skip the row."""
    xmin, ymin, xmax, ymax, assets, links, item_id = row
    corners = (xmin, ymin, xmax, ymax)
    box: Bbox | None = None
    if all(isinstance(value, (int, float)) for value in corners):
        box = (float(xmin), float(ymin), float(xmax), float(ymax))
    if not scopes.intersects(box, scope):
        return None
    base = self_href(parse_json(links), url)
    if base is None:
        base = items.get(str(item_id), url) if item_id is not None else url
    return cog_href(assets_of({"assets": parse_json(assets)}, base))


def self_href(links: object, base: str) -> str | None:
    """Return the item's ``self`` link resolved against ``base``, or None without one."""
    if not isinstance(links, list):
        return None
    hrefs = [
        link.get("href") for link in links if isinstance(link, dict) and link.get("rel") == "self"
    ]
    href = next((href for href in hrefs if isinstance(href, str) and href), None)
    if href is None:
        return None
    try:
        return asset_href(href, base)
    except ValueError:
        return None
