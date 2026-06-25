from __future__ import annotations

import geopandas as gpd


def geometry_quality_summary(gdf: gpd.GeoDataFrame) -> dict[str, int]:
    return {
        "row_count": int(len(gdf)),
        "empty_geometry_count": int(gdf.geometry.is_empty.sum()),
        "invalid_geometry_count": int((~gdf.geometry.is_valid).sum()),
        "missing_geometry_count": int(gdf.geometry.isna().sum()),
    }
