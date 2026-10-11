"""Add many COG files as one raster layer.

A collection of COG items becomes a GDAL VRT over the ``/vsicurl/`` paths
of the items that intersect the scope. QGIS reads only the tiles on screen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Executor

    from qgis.core import QgsRasterLayer

    from portolan_registry_qgis.core.download import FetchJson
    from portolan_registry_qgis.core.scope import Scope
    from portolan_registry_qgis.core.stac import Document

# The layer keeps the COG URLs here, one per line, so a saved project can
# build the VRT again.
SOURCES_PROPERTY = "portolan/mosaic_sources"


def item_hrefs_in(
    collection: Document,
    scope: Scope,
    fetch_json: FetchJson,
    executor: Executor | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> list[str]:
    """Return the COG hrefs of the collection's items that intersect ``scope``.

    A ``collection-mirror`` asset (stac-geoparquet) answers with one DuckDB
    query when DuckDB is present. Otherwise the items are read with
    ``download.walk`` and filtered by bbox. Safe off the main thread.
    """
    raise NotImplementedError("Stage 1 qgis_io stream")


def build_mosaic(hrefs: list[str], name: str) -> QgsRasterLayer:
    """Write a VRT over ``hrefs`` to the scratch folder and open it as a layer.

    Raises:
        layers.LayerError: GDAL cannot build the VRT, or QGIS cannot open it.
    """
    raise NotImplementedError("Stage 1 qgis_io stream")
