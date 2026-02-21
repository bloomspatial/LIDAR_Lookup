"""
List 3DEP LIDAR LAZ file URLs for a bounding box or GPS point.

Uses the National Map 3DEP Elevation Index API; no download or S3.
Input: bbox (tuple/list/dict/JSON) or a single (lon, lat) point.
"""

from lidar_lookup.api import (
    get_default_index_path,
    list_lidar_urls,
    list_lidar_urls_from_index,
    load_index_bbox_filter,
    lpc_link_to_laz_urls,
    parse_bbox,
    point_to_bbox,
    query_3dep_index,
    query_3dep_index_by_point,
    suggest_swap_bbox,
    suggest_swap_point,
)

__all__ = [
    "get_default_index_path",
    "list_lidar_urls",
    "list_lidar_urls_from_index",
    "load_index_bbox_filter",
    "lpc_link_to_laz_urls",
    "parse_bbox",
    "point_to_bbox",
    "query_3dep_index",
    "query_3dep_index_by_point",
    "suggest_swap_bbox",
    "suggest_swap_point",
]
__version__ = "0.1.0"
