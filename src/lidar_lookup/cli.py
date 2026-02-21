"""
CLI: list LAZ URLs for a bbox (JSON or numeric) or GPS point.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from lidar_lookup.api import list_lidar_urls, suggest_swap_bbox, suggest_swap_point


LAZ_LAS = (".laz", ".las")

def main() -> int:
    parser = argparse.ArgumentParser(
        description="List 3DEP LIDAR LAZ file URLs for a bounding box or GPS point (no download)."
    )
    parser.add_argument(
        "source",
        nargs="*",
        default=None,
        help="JSON file path or '-' for bbox; or one or more .laz/.las files to display (no --display needed).",
    )
    parser.add_argument(
        "--bbox",
        metavar=("MINX", "MINY", "MAXX", "MAXY"),
        nargs=4,
        type=float,
        default=None,
        help="WGS84 bounding box: minx miny maxx maxy (lon, lat).",
    )
    parser.add_argument(
        "--json",
        metavar="JSON",
        default=None,
        help="JSON bbox: file path, or literal e.g. '[minx,miny,maxx,maxy]' or '{\"bbox\":[...]}'.",
    )
    parser.add_argument(
        "--point",
        metavar=("LON", "LAT"),
        nargs=2,
        type=float,
        default=None,
        help="Single point (lon lat). A small bbox is used around it.",
    )
    parser.add_argument(
        "--point-buffer",
        type=float,
        default=0.0,
        metavar="DEGREES",
        help="With --point: buffer in degrees to expand to a bbox (default 0 = exact point query, like url.sh). Use e.g. 0.001 for ~220m area.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write URLs (or filenames if --filenames) to FILE, one per line.",
    )
    parser.add_argument(
        "--filenames",
        action="store_true",
        help="Output only filenames (e.g. file.laz) instead of full URLs.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download each LAZ file to the current directory (or --download-dir).",
    )
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="With --download: directory to save files (default: current directory).",
    )
    filter_grp = parser.add_mutually_exclusive_group()
    filter_grp.add_argument(
        "--filter-tiles",
        action="store_true",
        dest="filter_tiles",
        help="Filter to tiles that intersect the query (default; uses per-project index).",
    )
    filter_grp.add_argument(
        "--no-filter-tiles",
        action="store_false",
        dest="filter_tiles",
        help="List all LAZ files in each project (no bbox filter).",
    )
    parser.set_defaults(filter_tiles=None)  # None = default True (filter by bbox)
    parser.add_argument(
        "--display",
        metavar="LAZ_FILE",
        type=Path,
        nargs="+",
        default=None,
        help="Display one or more LAZ/LAS files in a 3D viewer (requires pip install lidar-lookup[display]).",
    )
    parser.add_argument(
        "--decimate",
        type=int,
        default=None,
        metavar="N",
        help="Plot every Nth point (default: auto-decimate to ~10M points). Use 1 for all points.",
    )
    parser.add_argument(
        "--no-local-index",
        action="store_true",
        dest="no_local_index",
        help="Skip local index and query the 3DEP API (use when the index does not cover your area).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
        # Reduce HTTP chatter so our DEBUG messages are readable
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("requests").setLevel(logging.WARNING)

    # Display mode: from --display or from positionals when all are .laz/.las
    positionals = [Path(p) for p in args.source] if args.source else []
    all_laz_las = bool(positionals) and all(
        p.suffix.lower() in LAZ_LAS for p in positionals
    )
    display_files = args.display if args.display is not None else (positionals if all_laz_las else None)

    if display_files is not None:
        missing = [p for p in display_files if not p.exists()]
        if missing:
            print(f"Error: not found: {missing}", file=sys.stderr)
            return 1
        try:
            from lidar_lookup.display import display_laz
        except ImportError as e:
            print(
                "Error: display requires optional dependencies. Install with:\n  pip install lidar-lookup[display]",
                file=sys.stderr,
            )
            if args.verbose:
                print(f"  {e}", file=sys.stderr)
            return 1
        try:
            display_laz(display_files, decimate=args.decimate)
        except Exception as e:
            print(f"Error displaying: {e}", file=sys.stderr)
            return 1
        return 0

    # Exactly one of: bbox, json, point, or a single JSON source (file path or "-")
    json_source = None
    if len(positionals) == 1:
        s = args.source[0]
        if s == "-":
            json_source = "-"
        else:
            p = positionals[0]
            if p.suffix.lower() not in LAZ_LAS:
                json_source = p
    inputs = [args.bbox, args.json, args.point, json_source]
    if sum(1 for x in inputs if x is not None) != 1:
        parser.error(
            "Provide exactly one of: --bbox, --json, --point, one or more .laz/.las files (to display), or a JSON file path (positional source)."
        )

    # Default: filter to tiles that intersect the query (per-project index); use --no-filter-tiles to list all
    if args.filter_tiles is None:
        args.filter_tiles = True
    filter_tiles = args.filter_tiles

    use_local_index = not args.no_local_index
    if args.bbox is not None:
        bbox = tuple(args.bbox)
        swapped = suggest_swap_bbox(*bbox)
        if swapped is not None:
            print(
                "Warning: lat/lon may be flipped (lat must be -90..90). Did you mean: --bbox %.6f %.6f %.6f %.6f?"
                % swapped,
                file=sys.stderr,
            )
        urls = list_lidar_urls(bbox, filter_tiles_by_bbox=filter_tiles, use_local_index=use_local_index)
    elif args.point is not None:
        lon, lat = args.point[0], args.point[1]
        swapped = suggest_swap_point(lon, lat)
        if swapped is not None:
            print(
                "Warning: lat/lon may be flipped (lat must be -90..90). Did you mean: --point %.6f %.6f?"
                % swapped,
                file=sys.stderr,
            )
        urls = list_lidar_urls(
            (lon, lat),
            point_buffer_degrees=args.point_buffer,
            filter_tiles_by_bbox=filter_tiles,
            use_local_index=use_local_index,
        )
    elif args.json is not None:
        raw = args.json.strip()
        if raw.startswith("{") or raw.startswith("["):
            source: str | Path = raw
        else:
            source = Path(raw)
            if not source.exists():
                print(f"Error: not found: {source}", file=sys.stderr)
                return 1
        urls = list_lidar_urls(source, filter_tiles_by_bbox=filter_tiles, use_local_index=use_local_index)
    else:
        assert json_source is not None
        if json_source == "-":
            source = json.load(sys.stdin)
        else:
            source = json_source
            if not source.exists():
                print(f"Error: not found: {source}", file=sys.stderr)
                return 1
        urls = list_lidar_urls(source, filter_tiles_by_bbox=filter_tiles, use_local_index=use_local_index)

    if args.filenames:
        lines = sorted(set(u.split("/")[-1].split("?")[0] for u in urls))
    else:
        lines = urls

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote {len(lines)} line(s) to {args.output}", file=sys.stderr)
    else:
        for line in lines:
            print(line)

    if args.download and urls:
        download_dir = args.download_dir.resolve()
        download_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            path = urlparse(url).path
            name = path.split("/")[-1].split("?")[0] or "download.laz"
            dest = download_dir / name
            if dest.exists():
                if args.verbose:
                    print(f"Skipping {dest} (already exists)", file=sys.stderr)
                continue
            if args.verbose:
                print(f"Downloading {url} -> {dest}", file=sys.stderr)
            try:
                r = requests.get(url, stream=True, timeout=60)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                print(f"Downloaded {dest}", file=sys.stderr)
            except Exception as e:
                print(f"Error downloading {url}: {e}", file=sys.stderr)

    if len(lines) == 0:
        print("No LAZ URLs found for this location.", file=sys.stderr)

    if args.verbose:
        import logging
        logging.getLogger("lidar_lookup.cli").debug("total returned: %d", len(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
