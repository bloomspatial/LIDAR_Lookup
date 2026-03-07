"""
Core API: 3DEP index query, LPC link resolution, and listing LAZ URLs for a bbox or point.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

_log = logging.getLogger(__name__)

# 3DEP Elevation Index API (bbox query)
THREEDEP_INDEX_URL = (
    "https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/24/query"
)
FILE_LINKS_SUFFIX = "/0_file_download_links.txt"

_NETWORK_RETRY_ATTEMPTS = 3
_NETWORK_RETRY_BACKOFF_SEC = 1.0

# Default buffer (degrees) when a single point is given (~220 m at mid-latitudes)
DEFAULT_POINT_BUFFER_DEGREES = 0.001

# When filtering by bbox on 3DEP path: max parallel workers for downloading project metadata XMLs
_METADATA_DOWNLOAD_WORKERS = 10

# WGS84 bounds for flip detection
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LON_MIN, _LON_MAX = -180.0, 180.0


def suggest_swap_point(x: float, y: float) -> tuple[float, float] | None:
    """
    If (x, y) looks like (lat, lon) instead of (lon, lat), return (lon, lat) with values swapped.

    Latitude must be in [-90, 90]; longitude in [-180, 180]. When the second value is outside
    [-90, 90] and the first is inside it, we propose interpreting as (lat, lon) and return (y, x).
    """
    y_abs = abs(y)
    x_abs = abs(x)
    if y_abs > _LAT_MAX and x_abs <= _LAT_MAX and y_abs <= _LON_MAX:
        return (y, x)
    return None


def suggest_swap_bbox(
    minx: float, miny: float, maxx: float, maxy: float
) -> tuple[float, float, float, float] | None:
    """
    If bbox (minx, miny, maxx, maxy) looks like (minlon, minlat, maxlon, maxlat) with axes swapped,
    return the corrected bbox (miny, minx, maxy, maxx) as (minx, miny, maxx, maxy).
    """
    # lat (y) must be in [-90, 90]; if y range is outside that but x range is in [-90, 90], likely swapped
    y_invalid = (miny < _LAT_MIN or maxy > _LAT_MAX) and (_LON_MIN <= miny <= _LON_MAX and _LON_MIN <= maxy <= _LON_MAX)
    x_looks_lat = _LAT_MIN <= minx <= _LAT_MAX and _LAT_MIN <= maxx <= _LAT_MAX
    if y_invalid and x_looks_lat:
        return (miny, minx, maxy, maxx)
    return None


# Per-project index cache: LIDAR_CACHE_DIR (default inputs/cache); each project stored as <sha256(metadata_url)>.json
def _get_project_index_cache_path(metadata_url: str) -> Path:
    """Return the local JSON path for a per-project metadata index (for caching)."""
    cache_dir = Path(os.environ.get("LIDAR_CACHE_DIR", "inputs/cache"))
    key = hashlib.sha256(metadata_url.encode()).hexdigest()[:24]
    return cache_dir / f"{key}.json"


def _get_with_retry(url: str, **kwargs: Any) -> requests.Response:
    """GET with retries on ConnectionError / RequestException."""
    last: requests.RequestException | None = None
    for attempt in range(_NETWORK_RETRY_ATTEMPTS):
        try:
            return requests.get(url, **kwargs)
        except requests.RequestException as e:
            last = e
            if attempt == _NETWORK_RETRY_ATTEMPTS - 1:
                raise
            delay = _NETWORK_RETRY_BACKOFF_SEC * (2**attempt)
            _log.warning(
                "Request failed (attempt %s/%s), retrying in %.1fs: %s",
                attempt + 1,
                _NETWORK_RETRY_ATTEMPTS,
                delay,
                e,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def _bbox_intersects_wgs84(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    """True if bounding boxes a and b (minx, miny, maxx, maxy) overlap."""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def load_index_bbox_filter(
    index_path: str | Path,
    bbox: tuple[float, float, float, float],
) -> set[str]:
    """
    Load a metadata index JSON and return the set of LAZ filenames whose bbox intersects the query.

    Index format: dict mapping filename to {west, east, north, south, url?} (e.g. per-project
    cache). No network calls; in-memory bbox intersection only.

    Args:
        index_path: Path to the JSON index file (e.g. inputs/laz_metadata_index.json).
        bbox: Query bbox (minx, miny, maxx, maxy) in WGS84 (lon, lat).

    Returns:
        Set of LAZ filenames (e.g. "tile_12.laz") whose tile extent intersects bbox.
    """
    path = Path(index_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for filename, entry in data.items():
        w = entry.get("west")
        e = entry.get("east")
        n = entry.get("north")
        s = entry.get("south")
        if None in (w, e, n, s):
            continue
        tile_bbox = (float(w), float(s), float(e), float(n))  # minx, miny, maxx, maxy
        if _bbox_intersects_wgs84(bbox, tile_bbox):
            out.add(filename)
    return out


def list_lidar_urls_from_index(
    index_path: str | Path,
    bbox: tuple[float, float, float, float],
) -> list[str]:
    """
    List LAZ file URLs that intersect the bbox using a local metadata index.

    Returns only entries that have an "url" key. No network calls.

    Args:
        index_path: Path to the JSON index file.
        bbox: Query bbox (minx, miny, maxx, maxy) in WGS84 (lon, lat).

    Returns:
        List of full LAZ URLs whose tile extent intersects bbox, sorted alphabetically.
    """
    path = Path(index_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    urls: list[str] = []
    for filename, entry in data.items():
        w = entry.get("west")
        e = entry.get("east")
        n = entry.get("north")
        s = entry.get("south")
        url = entry.get("url")
        if None in (w, e, n, s) or not url:
            continue
        tile_bbox = (float(w), float(s), float(e), float(n))
        if _bbox_intersects_wgs84(bbox, tile_bbox):
            urls.append(url)
    return sorted(urls)


def get_default_index_path() -> Path:
    """Return the default index path (from LIDAR_INDEX_PATH env or inputs/laz_metadata_index.json)."""
    default = os.environ.get("LIDAR_INDEX_PATH", "inputs/laz_metadata_index.json")
    return Path(default)


def _filter_index_by_bbox(
    index: dict[str, dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> list[str]:
    """Return LAZ URLs from index whose tile extent intersects bbox."""
    out: list[str] = []
    for _filename, entry in index.items():
        w = entry.get("west")
        e = entry.get("east")
        n = entry.get("north")
        s = entry.get("south")
        url = entry.get("url")
        if None in (w, e, n, s) or not url:
            continue
        tile_bbox = (float(w), float(s), float(e), float(n))
        if _bbox_intersects_wgs84(bbox, tile_bbox):
            out.append(url)
    return out


def _project_urls_filtered_by_metadata_index(
    lpc_link: str,
    project_urls: list[str],
    bbox: tuple[float, float, float, float],
    timeout: int,
) -> list[str]:
    """
    Build a per-project index from rockyweb XML metadata and return only LAZ URLs
    whose tile extent (from metadata) intersects the query bbox. No LAZ file fetches.
    Per-project indexes are cached under LIDAR_CACHE_DIR (default inputs/cache).
    """
    from lidar_lookup.metadata_indexer import (
        build_searchable_index,
        download_metadata_for_dir,
        write_index_json,
    )

    laz_url_map = {}
    for u in project_urls:
        name = Path(urllib.parse.urlparse(u).path).name.split("?")[0]
        laz_url_map[name] = u
    metadata_url = lpc_link.rstrip("/") + "/metadata"
    cache_path = _get_project_index_cache_path(metadata_url)

    if cache_path.exists():
        _log.debug("per-project index file found --- %s", cache_path)
        index = json.loads(cache_path.read_text(encoding="utf-8"))
        return _filter_index_by_bbox(index, bbox)

    _log.debug("per-project index file missing --- %s", cache_path)
    _log.info("Index not found -- building. (this will take a while)")
    _log.debug("Building index from XML metadata: %s", metadata_url)
    with tempfile.TemporaryDirectory(prefix="lidar_lookup_") as tmpdir:
        tmp = Path(tmpdir)
        n_xml = download_metadata_for_dir(
            metadata_url,
            tmp,
            max_workers=_METADATA_DOWNLOAD_WORKERS,
            timeout=timeout,
        )
        if n_xml == 0:
            _log.debug("No metadata XMLs at %s; including all project URLs", metadata_url)
            return [u for u in project_urls]
        index = build_searchable_index(tmp, laz_url_map=laz_url_map)
        if not index:
            return [u for u in project_urls]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_index_json(index, cache_path)
        _log.debug("per-project index file found --- %s", cache_path)
    return _filter_index_by_bbox(index, bbox)


def parse_bbox(
    source: str | Path | dict[str, Any] | list[float] | tuple[float, ...],
) -> tuple[float, float, float, float]:
    """
    Parse a bbox from JSON (file path, JSON string, dict, or list) or a 4-tuple.

    Supported formats:
    - (minx, miny, maxx, maxy) or [minx, miny, maxx, maxy]
    - {"minx": n, "miny": n, "maxx": n, "maxy": n}
    - {"bbox": [minx, miny, maxx, maxy]}
    - {"type": "bbox", "coordinates": [minx, miny, maxx, maxy]}
    - Path to a .json file or JSON string containing any of the above

    Returns:
        (minx, miny, maxx, maxy) in WGS84 (lon, lat).
    """
    if isinstance(source, (tuple, list)):
        if len(source) != 4:
            raise ValueError("bbox must have 4 numbers: minx, miny, maxx, maxy")
        return (float(source[0]), float(source[1]), float(source[2]), float(source[3]))

    if isinstance(source, dict):
        data = source
    elif isinstance(source, (str, Path)):
        path = Path(source)
        if path.suffix.lower() == ".json" and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = json.loads(source)
    else:
        raise TypeError("source must be bbox tuple/list, dict, JSON path, or JSON string")

    if isinstance(data, list):
        if len(data) != 4:
            raise ValueError("bbox array must have 4 numbers: minx, miny, maxx, maxy")
        return (float(data[0]), float(data[1]), float(data[2]), float(data[3]))

    if not isinstance(data, dict):
        raise ValueError("JSON must be an object or 4-element array")

    if "bbox" in data:
        coords = data["bbox"]
        if len(coords) != 4:
            raise ValueError("bbox must have 4 numbers: minx, miny, maxx, maxy")
        return (float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3]))
    if "coordinates" in data and data.get("type") == "bbox":
        coords = data["coordinates"]
        if len(coords) != 4:
            raise ValueError("coordinates must have 4 numbers: minx, miny, maxx, maxy")
        return (float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3]))
    if "minx" in data and "miny" in data and "maxx" in data and "maxy" in data:
        return (
            float(data["minx"]),
            float(data["miny"]),
            float(data["maxx"]),
            float(data["maxy"]),
        )

    raise ValueError(
        "JSON must contain 'bbox', 'coordinates' (with type 'bbox'), or minx/miny/maxx/maxy"
    )


def point_to_bbox(
    lon: float,
    lat: float,
    buffer_degrees: float = DEFAULT_POINT_BUFFER_DEGREES,
) -> tuple[float, float, float, float]:
    """
    Create a bbox centered on (lon, lat) with the given buffer in degrees.

    Returns:
        (minx, miny, maxx, maxy) in WGS84.
    """
    return (
        lon - buffer_degrees,
        lat - buffer_degrees,
        lon + buffer_degrees,
        lat + buffer_degrees,
    )


def _project_year_from_lpc_link(lpc_link: str) -> int:
    """
    Extract a representative year from a 3DEP project URL/path for ordering.

    Looks for:
    - 4-digit years 1990-2030 (e.g. CA_NoCal_2018, legacy/..._2004).
    - Bxx pattern (e.g. B23, B24) interpreted as 20xx (USGS batch/fiscal style).
    Returns the maximum such year found, or 0 if none (unknown-year projects sort last).
    """
    years = [int(m) for m in re.findall(r"\b(19[90]\d|20[0-2]\d|2030)\b", lpc_link)]
    # B23, B24 etc. (e.g. CA_SanFrancisco_B23 or .../B16). \b doesn't work before B after _
    b_years = [2000 + int(m) for m in re.findall(r"(?:^|[_/])B(\d{2})\b", lpc_link, re.IGNORECASE)]
    all_years = years + b_years
    return max(all_years) if all_years else 0


def _project_sort_key(lpc_link: str) -> tuple[int, int]:
    """
    Sort key for choosing newest project: (year, legacy_penalty).
    Higher year wins; non-legacy wins over legacy when years are equal.
    """
    year = _project_year_from_lpc_link(lpc_link)
    # Paths under /legacy/ are deprioritized so we prefer newer non-legacy projects
    legacy_penalty = 0 if "/legacy/" not in (lpc_link or "") else -1
    return (year, legacy_penalty)


def _pick_newest_project(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    When multiple 3DEP projects cover a point/area, return a single-element list
    containing the project whose URL suggests the newest collection year.

    This avoids fetching LAZ file lists from many overlapping projects (e.g. legacy
    plus newer) and prefers the most recent data. Legacy paths are deprioritized.
    """
    if len(features) <= 1:
        return features
    best = max(
        features,
        key=lambda f: _project_sort_key(f.get("lpc_link") or ""),
    )
    _log.debug(
        "picked single project (newest by year in path): %s",
        best.get("lpc_link", ""),
    )
    return [best]


def _parse_3dep_features(data: dict) -> list[dict[str, Any]]:
    """Parse 3DEP API response into list of feature dicts with lpc_link, etc."""
    features = data.get("features") or []
    out = []
    for f in features:
        attrs = f.get("attributes") or {}
        lpc_link = attrs.get("lpc_link")
        if not lpc_link or not isinstance(lpc_link, str):
            continue
        out.append({
            "lpc_link": lpc_link.strip().rstrip("/"),
            "workunit": attrs.get("workunit"),
            "collect_end": attrs.get("collect_end"),
            "ql": attrs.get("ql"),
        })
    return out


def query_3dep_index_by_point(
    lon: float,
    lat: float,
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """
    Query the 3DEP Elevation Index API by a single point (same as url.sh).

    Returns only index features whose footprint contains the point. Use this for
    exact-point lookups; for an area use query_3dep_index with a bbox.

    Args:
        lon: Longitude (WGS84).
        lat: Latitude (WGS84).
        timeout: Request timeout in seconds.

    Returns:
        List of feature dicts with at least "lpc_link".
    """
    _log.debug("query_3dep_index_by_point: lon=%.6f lat=%.6f", lon, lat)
    geometry = {
        "x": lon,
        "y": lat,
        "spatialReference": {"wkid": 4326},
    }
    geometry_encoded = urllib.parse.quote(json.dumps(geometry))
    url = (
        f"{THREEDEP_INDEX_URL}?f=json&returnGeometry=true&returnTrueCurves=false"
        f"&spatialRel=esriSpatialRelIntersects&geometryType=esriGeometryPoint"
        f"&inSR=4326&outSR=4326&outFields=*&geometry={geometry_encoded}"
    )
    resp = _get_with_retry(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"3DEP API error: {data['error']}")
    out = _parse_3dep_features(data)
    _log.debug("3DEP index returned %d project(s) containing point", len(out))
    for feat in out:
        _log.debug("  project: %s", feat.get("lpc_link", ""))
    return out


def query_3dep_index(
    bbox_wgs84_tuple: tuple[float, float, float, float],
    timeout: int = 60,
) -> list[dict[str, Any]]:
    """
    Query the 3DEP Elevation Index API by bounding box.

    Args:
        bbox_wgs84_tuple: (minx, miny, maxx, maxy) in WGS84 (lon, lat).
        timeout: Request timeout in seconds.

    Returns:
        List of feature dicts with at least "lpc_link". Each dict has
        keys like "lpc_link", "workunit", "collect_end", "ql".
    """
    minx, miny, maxx, maxy = bbox_wgs84_tuple
    _log.debug(
        "query_3dep_index bbox: minx=%.6f miny=%.6f maxx=%.6f maxy=%.6f",
        minx, miny, maxx, maxy,
    )
    geometry = {
        "xmin": minx,
        "ymin": miny,
        "xmax": maxx,
        "ymax": maxy,
        "spatialReference": {"wkid": 4326},
    }
    geometry_encoded = urllib.parse.quote(json.dumps(geometry))
    url = (
        f"{THREEDEP_INDEX_URL}?f=json&returnGeometry=true&returnTrueCurves=false"
        f"&spatialRel=esriSpatialRelIntersects&geometryType=esriGeometryEnvelope"
        f"&inSR=4326&outSR=4326&outFields=*&geometry={geometry_encoded}"
    )
    resp = _get_with_retry(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"3DEP API error: {data['error']}")
    features = data.get("features") or []
    parsed = _parse_3dep_features(data)
    _log.debug("3DEP index returned %d project(s) intersecting bbox", len(parsed))
    for feat in parsed:
        _log.debug("  project: %s", feat.get("lpc_link", ""))
    return parsed


def lpc_link_to_laz_urls(
    lpc_link: str,
    timeout: int = 60,
) -> list[str]:
    """
    Resolve a rockyweb directory URL to a list of LAZ file URLs.

    Fetches {lpc_link}/0_file_download_links.txt and parses lines
    that look like direct .laz URLs.

    Args:
        lpc_link: Base URL from 3DEP API attributes.lpc_link (no trailing slash).
        timeout: Request timeout in seconds.

    Returns:
        List of full LAZ URLs. May be empty if the file is missing or has no .laz lines.
    """
    url = lpc_link.rstrip("/") + FILE_LINKS_SUFFIX
    try:
        resp = _get_with_retry(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        _log.warning("Failed to fetch file links from %s: %s", url, e)
        return []
    urls = []
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if line.startswith("http") and ".laz" in line and line.rstrip("/").endswith(".laz"):
            urls.append(line)
    _log.debug("lpc_link_to_laz_urls %s -> %d LAZ file(s)", lpc_link, len(urls))
    return urls


def list_lidar_urls(
    source: (
        tuple[float, float, float, float]
        | tuple[float, float]
        | str
        | Path
        | dict[str, Any]
        | list[float]
    ),
    *,
    point_buffer_degrees: float = DEFAULT_POINT_BUFFER_DEGREES,
    timeout: int = 60,
    filter_tiles_by_bbox: bool = True,
    use_local_index: bool = True,
) -> list[str]:
    """
    List LAZ file URLs that cover the given area.

    Uses the 3DEP API to find project(s), then rockyweb file links. When
    filter_tiles_by_bbox is True, per-project metadata indexes (cached under
    LIDAR_CACHE_DIR) are used to return only LAZ URLs whose tile extent
    intersects the bbox.

    Args:
        source: One of:
            - Bbox (minx, miny, maxx, maxy) in WGS84 as tuple or list
            - Single point (lon, lat) as 2-element tuple/list — expanded to a bbox
              using point_buffer_degrees
            - Path to a .json file or JSON string with a bbox (see parse_bbox)
            - Dict with bbox (see parse_bbox)
        point_buffer_degrees: Buffer in degrees when source is a single point (ignored
            when source is a bbox). Default 0 = exact point query (fewer results, like url.sh).
            Use e.g. 0.001 for a ~220 m area around the point.
        timeout: Timeout in seconds for 3DEP and rockyweb requests.
        filter_tiles_by_bbox: If True (default), fetch XML metadata per project, build a
            per-project index, and return only URLs whose tile extent intersects the bbox.
            If False, return all LAZ URLs for the selected project(s).
        use_local_index: Unused; kept for backward compatibility.

    Returns:
        List of unique LAZ URLs (no download, no S3), sorted alphabetically.
    """
    # Resolve bbox from source (no network yet)
    if isinstance(source, (tuple, list)) and len(source) == 2:
        lon, lat = float(source[0]), float(source[1])
        swapped = suggest_swap_point(lon, lat)
        if swapped is not None:
            _log.warning(
                "Lat/lon may be flipped (lat must be -90..90). Did you mean (lon, lat) = (%.6f, %.6f)?",
                swapped[0], swapped[1],
            )
        if point_buffer_degrees == 0:
            bbox = (lon, lat, lon, lat)
        else:
            bbox = point_to_bbox(lon, lat, buffer_degrees=point_buffer_degrees)
            _log.debug(
                "point (%.6f, %.6f) with buffer %.4f deg -> bbox (%.6f, %.6f, %.6f, %.6f)",
                lon, lat, point_buffer_degrees, *bbox,
            )
    elif isinstance(source, (tuple, list)) and len(source) == 4:
        minx, miny, maxx, maxy = float(source[0]), float(source[1]), float(source[2]), float(source[3])
        swapped = suggest_swap_bbox(minx, miny, maxx, maxy)
        if swapped is not None:
            _log.warning(
                "Bbox lat/lon may be flipped. Did you mean (minx, miny, maxx, maxy) = (%.6f, %.6f, %.6f, %.6f)?",
                *swapped,
            )
        bbox = (minx, miny, maxx, maxy)
    else:
        bbox = parse_bbox(source)
        swapped = suggest_swap_bbox(*bbox)
        if swapped is not None:
            _log.warning(
                "Bbox lat/lon may be flipped. Did you mean (minx, miny, maxx, maxy) = (%.6f, %.6f, %.6f, %.6f)?",
                *swapped,
            )

    # Use 3DEP API and rockyweb file links; per-project metadata indexes when filter_tiles_by_bbox
    if isinstance(source, (tuple, list)) and len(source) == 2:
        lon, lat = float(source[0]), float(source[1])
        if point_buffer_degrees == 0:
            features = query_3dep_index_by_point(lon, lat, timeout=timeout)
        else:
            features = query_3dep_index(bbox, timeout=timeout)
    elif isinstance(source, (tuple, list)) and len(source) == 4:
        features = query_3dep_index(bbox, timeout=timeout)
    else:
        features = query_3dep_index(bbox, timeout=timeout)
    features = _pick_newest_project(features)
    seen: set[str] = set()
    urls = []
    for feat in features:
        lpc = feat.get("lpc_link")
        if not lpc:
            continue
        project_urls = lpc_link_to_laz_urls(lpc, timeout=timeout)
        if filter_tiles_by_bbox:
            filtered = _project_urls_filtered_by_metadata_index(
                lpc, project_urls, bbox, timeout
            )
            kept = 0
            for url in filtered:
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
                    kept += 1
            if kept:
                _log.debug(
                    "project %s: %d LAZ URL(s) after metadata-index bbox filter (%d new)",
                    lpc, kept, kept,
                )
        else:
            new_count = sum(1 for u in project_urls if u not in seen)
            for url in project_urls:
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
            if new_count:
                _log.debug(
                    "project %s: %d LAZ URL(s) (%d new)",
                    lpc, len(project_urls), new_count,
                )
    if filter_tiles_by_bbox:
        _log.debug("per-project index used for bbox filter")
    else:
        _log.debug(
            "no per-project index used; listing all LAZ URLs"
        )
    _log.debug("total unique LAZ URLs: %d", len(urls))
    return sorted(urls)
