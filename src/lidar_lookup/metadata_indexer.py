"""
Local metadata index builder for LAZ tile lookup.

Builds a searchable index from USGS rockyweb metadata XMLs and 0_file_download_links.txt:
- Bbox per tile is read from FGDC-style metadata XML (<bounding> / westbc, eastbc, northbc, southbc).
- LAZ filename is derived from XML filename (e.g. ..._12.xml → ..._12.laz).
- Optional URL map from 0_file_download_links.txt (filename → full LAZ URL).

Index format: dict[filename, {west, east, north, south, url?}] for use with load_index_bbox_filter().
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests

from lidar_lookup.api import _get_with_retry

FILE_LINKS_FILENAME = "0_file_download_links.txt"
_log = logging.getLogger(__name__)


def parse_bounding_box(source: str | Path | bytes) -> tuple[float, float, float, float] | None:
    """
    Parse bounding box from FGDC-style metadata XML.

    Looks for <bounding> with <westbc>, <eastbc>, <northbc>, <southbc>, or
    FGDC CSDGM elements like West_Bounding_Coordinate, etc.

    Args:
        source: Path to .xml file, XML string, or bytes.

    Returns:
        (west, east, north, south) in decimal degrees, or None if not found.
    """
    if isinstance(source, Path):
        content = source.read_bytes()
    elif isinstance(source, str) and (source.lstrip().startswith("<") or Path(source).suffix.lower() == ".xml"):
        if Path(source).exists():
            content = Path(source).read_bytes()
        else:
            content = source.encode("utf-8")
    elif isinstance(source, bytes):
        content = source
    else:
        content = source.encode("utf-8") if isinstance(source, str) else source

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None

    # Try short form first (e.g. USGS rockyweb metadata): <bounding><westbc>, etc.
    for tag in ("bounding", "idinfo", "spdom"):
        for parent in root.iter():
            if _local_tag(parent.tag) != tag:
                continue
            west = _text_of(parent, "westbc") or _text_of(parent, "West_Bounding_Coordinate")
            east = _text_of(parent, "eastbc") or _text_of(parent, "East_Bounding_Coordinate")
            north = _text_of(parent, "northbc") or _text_of(parent, "North_Bounding_Coordinate")
            south = _text_of(parent, "southbc") or _text_of(parent, "South_Bounding_Coordinate")
            if all(x is not None for x in (west, east, north, south)):
                try:
                    return (float(west), float(east), float(north), float(south))
                except (TypeError, ValueError):
                    pass
    # FGDC: any element with West_Bounding_Coordinate etc. anywhere in tree
    west = _text_of_any(root, ["West_Bounding_Coordinate", "westbc"])
    east = _text_of_any(root, ["East_Bounding_Coordinate", "eastbc"])
    north = _text_of_any(root, ["North_Bounding_Coordinate", "northbc"])
    south = _text_of_any(root, ["South_Bounding_Coordinate", "southbc"])
    if all(x is not None for x in (west, east, north, south)):
        try:
            return (float(west), float(east), float(north), float(south))
        except (TypeError, ValueError):
            pass
    return None


def _local_tag(tag: str) -> str:
    """Return local part of tag (strip XML namespace if present)."""
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text_of(parent: Any, tag: str) -> str | None:
    child = parent.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    for c in parent:
        if _local_tag(c.tag) == tag and c.text:
            return c.text.strip()
    return None


def _text_of_any(root: Any, tags: list[str]) -> str | None:
    for tag in tags:
        for elem in root.iter():
            if _local_tag(elem.tag) == tag and elem.text:
                return elem.text.strip()
    return None


def extract_filename_from_metadata_filename(metadata_path_or_name: str | Path) -> str:
    """
    Derive LAZ filename from metadata XML filename.

    Example: .../something_12.xml or something_12.xml → something_12.laz

    Args:
        metadata_path_or_name: Path or filename of the metadata XML.

    Returns:
        Basename with .laz extension (e.g. something_12.laz).
    """
    name = Path(metadata_path_or_name).name
    if name.lower().endswith(".xml"):
        name = name[:-4]
    return name + ".laz"


def download_file_links(
    base_url: str,
    timeout: int = 60,
) -> dict[str, str]:
    """
    Fetch 0_file_download_links.txt from a base URL and parse filename → full LAZ URL.

    Args:
        base_url: Base URL of the directory (no trailing slash).
        timeout: Request timeout in seconds.

    Returns:
        Dict mapping LAZ filename (basename) to full URL.
    """
    url = base_url.rstrip("/") + "/" + FILE_LINKS_FILENAME
    try:
        resp = _get_with_retry(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        _log.warning("Failed to fetch file links from %s: %s", url, e)
        return {}
    result = {}
    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if line.startswith("http") and ".laz" in line and line.rstrip("/").endswith(".laz"):
            result[Path(urllib.parse.urlparse(line).path).name.split("?")[0]] = line
    _log.debug("download_file_links %s -> %d entries", base_url, len(result))
    return result


def download_all_file_links(
    base_url: str,
    subdirs: list[str],
    timeout: int = 60,
) -> dict[str, str]:
    """
    Fetch file links from multiple subdirectories and merge (later overwrites earlier).

    Args:
        base_url: Base URL (no trailing slash).
        subdirs: List of subdirectory names (e.g. ['Batch4', 'Batch5']).
        timeout: Request timeout.

    Returns:
        Merged dict filename → URL.
    """
    merged: dict[str, str] = {}
    for sub in subdirs:
        sub_url = base_url.rstrip("/") + "/" + sub.strip("/")
        one = download_file_links(sub_url, timeout=timeout)
        merged.update(one)
    return merged


def _download_one_metadata(
    url: str,
    out_path: Path,
    timeout: int = 60,
) -> bool:
    try:
        resp = _get_with_retry(url, timeout=timeout)
        resp.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return True
    except Exception as e:
        _log.warning("Failed to download %s: %s", url, e)
        return False


def list_subdirectories(base_url: str, timeout: int = 60) -> list[str]:
    """
    List subdirectory names under base_url from an HTML directory listing.

    Fetches the base URL and parses hrefs that look like subdirectories
    (e.g. end with / or have no file extension). Returns list of subdir names (no trailing slash).
    """
    try:
        resp = _get_with_retry(base_url.rstrip("/") + "/", timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        _log.warning("Failed to list base URL %s: %s", base_url, e)
        return []
    # Match href like "Batch4/" or "Batch4" (subdir-like: no extension or ends with /)
    found = re.findall(r'href=["\']?([^"\'>\s]+?)["\']?/?\s*', resp.text, re.IGNORECASE)
    subdirs = []
    skip_names = {".", "..", "Parent", "Directory", "parent", "directory"}
    for s in found:
        s = s.strip().rstrip("/")
        base = s.split("/")[-1] if "/" in s else s
        if not base or base in skip_names or base in subdirs:
            continue
        # Skip obvious files (have a dot in the last segment)
        if "." in base and not base.startswith("."):
            continue
        subdirs.append(base)
    return subdirs


def download_metadata_for_dir(
    metadata_list_url: str,
    metadata_dir: Path,
    max_workers: int = 10,
    timeout: int = 60,
    skip_filename_containing: str | None = None,
) -> int:
    """
    Discover metadata XML URLs from a directory listing page and download them in parallel.

    Assumes the list_url is a rockyweb-style directory; we parse hrefs for .xml files.
    Optionally skip links whose path contains a given string (e.g. to exclude one acquisition set).

    Returns:
        Number of XML files successfully downloaded.
    """
    try:
        resp = _get_with_retry(metadata_list_url.rstrip("/") + "/", timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        _log.warning("Failed to list metadata directory %s: %s", metadata_list_url, e)
        return 0
    hrefs = re.findall(r'href=["\']?([^"\'>\s]+\.xml)', resp.text, re.IGNORECASE)
    if not hrefs:
        hrefs = re.findall(r'href=["\']?([^"\'>\s]+\.xml)', resp.text, re.IGNORECASE)
    urls_to_download: list[tuple[str, Path]] = []
    base = metadata_list_url.rstrip("/") + "/"
    for href in hrefs:
        href = href.strip()
        if skip_filename_containing and skip_filename_containing in href:
            continue
        if not href.startswith("http"):
            full_url = urllib.parse.urljoin(base, href)
        else:
            full_url = href
        name = Path(urllib.parse.urlparse(full_url).path).name
        urls_to_download.append((full_url, metadata_dir / name))
    if not urls_to_download:
        _log.warning("No .xml links found at %s", metadata_list_url)
        return 0
    total = len(urls_to_download)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    count = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_download_one_metadata, url, path, timeout): (url, path)
            for url, path in urls_to_download
        }
        for fut in as_completed(futures):
            if fut.result():
                count += 1
                if total <= 20 or count % 50 == 0 or count == total:
                    _log.debug("%d of %d downloaded", count, total)
    return count


def build_searchable_index(
    metadata_dir: Path,
    laz_url_map: dict[str, str] | None = None,
    skip_filename_containing: str | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Build the searchable index from metadata XML files in metadata_dir.

    Each XML is parsed for bbox; LAZ filename is derived from XML filename.

    Args:
        metadata_dir: Directory containing metadata .xml files.
        laz_url_map: Optional dict filename → full LAZ URL (from file links).
        skip_filename_containing: If set, skip XMLs whose path/filename contains this string.

    Returns:
        Dict mapping LAZ filename to {west, east, north, south, url?}.
    """
    index: dict[str, dict[str, Any]] = {}
    laz_url_map = laz_url_map or {}
    metadata_dir = Path(metadata_dir)
    if not metadata_dir.is_dir():
        return index
    xml_files = list(metadata_dir.glob("*.xml")) or list(metadata_dir.rglob("*.xml"))
    for xml_path in xml_files:
        if skip_filename_containing and skip_filename_containing in xml_path.name:
            continue
        bbox = parse_bounding_box(xml_path)
        if bbox is None:
            _log.debug("No bbox in %s", xml_path.name)
            continue
        west, east, north, south = bbox
        laz_name = extract_filename_from_metadata_filename(xml_path.name)
        entry: dict[str, Any] = {
            "west": west,
            "east": east,
            "north": north,
            "south": south,
        }
        if laz_name in laz_url_map:
            entry["url"] = laz_url_map[laz_name]
        index[laz_name] = entry
    return index


def write_index_json(index: dict[str, dict[str, Any]], out_path: Path) -> None:
    """Write index to a JSON file."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)


def write_index_sqlite(index: dict[str, dict[str, Any]], out_path: Path) -> None:
    """
    Write index to SQLite with an R-tree spatial index for bbox queries.

    Creates a table 'tiles' with (id, filename, west, east, north, south, url)
    and a virtual table 'tile_rtree' (id INTEGER, minx, maxx, miny, maxy) for
    2D bbox intersection. id is integer primary key for rtree compatibility.
    """
    import sqlite3
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out_path)
    conn.execute("DROP TABLE IF EXISTS tile_rtree")
    conn.execute("DROP TABLE IF EXISTS tiles")
    conn.execute(
        "CREATE TABLE tiles (id INTEGER PRIMARY KEY, filename TEXT UNIQUE, west REAL, east REAL, north REAL, south REAL, url TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE tile_rtree USING rtree(id, minx, maxx, miny, maxy)"
    )
    for rowid, (filename, entry) in enumerate(index.items(), start=1):
        west = entry["west"]
        east = entry["east"]
        north = entry["north"]
        south = entry["south"]
        url = entry.get("url") or ""
        conn.execute(
            "INSERT INTO tiles VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rowid, filename, west, east, north, south, url),
        )
        conn.execute(
            "INSERT INTO tile_rtree VALUES (?, ?, ?, ?, ?)",
            (rowid, west, east, south, north),
        )
    conn.commit()
    conn.close()
    _log.info("Wrote SQLite index to %s", out_path)
