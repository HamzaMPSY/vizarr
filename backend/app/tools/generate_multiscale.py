from __future__ import annotations

import argparse
import json
import logging
import time

from app.config import get_settings
from app.core.dataset_catalog import build_catalog_index
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.multiscale_builder import build_and_store_multiscale_pyramid
from app.core.oci_object_storage import OCIObjectStorageConnector


logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a durable OCI-backed multiscale Zarr store for browser-native rendering."
    )
    parser.add_argument(
        "--dataset-id",
        help="Optional encoded dataset id. If omitted and only one dataset is indexed, that dataset is used.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing multiscale store if it already exists.",
    )
    parser.add_argument(
        "--zarr-format",
        type=int,
        choices=(2, 3),
        default=2,
        help="Output multiscale store Zarr format. Default: 2.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=256,
        help="Output chunk size for y/x dimensions. Default: 256.",
    )
    parser.add_argument(
        "--min-size",
        type=int,
        default=256,
        help="Stop creating new overview levels once both spatial axes are at or below this size. Default: 256.",
    )
    parser.add_argument(
        "--max-browser-dimension",
        type=int,
        default=4096,
        help=(
            "Maximum height or width for multiscale level 0 when building an overview-first browser store. "
            "Default: 4096."
        ),
    )
    parser.add_argument(
        "--full-resolution",
        action="store_true",
        help="Copy the native-resolution source grid into multiscale level 0 instead of starting from a decimated overview.",
    )
    parser.add_argument(
        "--dtype",
        default="float32",
        help="Output numeric dtype for multiscale data arrays. Default: float32.",
    )
    parser.add_argument(
        "--prepopulate-through-zoom",
        type=int,
        default=None,
        help=(
            "Eagerly populate all pyramid levels up to and including this zoom during the build. "
            "If omitted, the builder chooses the highest zoom that fits within the tile budget."
        ),
    )
    parser.add_argument(
        "--prepopulate-tile-budget",
        type=int,
        default=128,
        help=(
            "When --prepopulate-through-zoom is omitted, eagerly populate as many low/mid zoom tiles "
            "as fit within this cumulative tile budget. Default: 128."
        ),
    )
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=None,
        help=(
            "Force the pyramid to build through this zoom even if the automatic tile-count ceiling would stop earlier. "
            "Use intentionally for high-detail datasets such as maize. Default: automatic."
        ),
    )
    parser.add_argument(
        "--min-token-ttl-seconds",
        type=int,
        default=1800,
        help=(
            "Abort before starting if the local OCI CLI token will expire sooner than this many seconds. "
            "Set to 0 to disable the preflight guard. Default: 1800."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging level. Default: INFO.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

    settings = get_settings()
    if settings.storage_backend != "oci_zarr":
        raise SystemExit("Multiscale generation requires STORAGE_BACKEND=oci_zarr")

    connector = OCIObjectStorageConnector(settings)
    _assert_minimum_token_ttl(connector, args.min_token_ttl_seconds)
    catalog = build_catalog_index(settings, connector)
    if not catalog:
        raise SystemExit("No OCI Zarr datasets were indexed for multiscale generation")

    entry = _select_entry(catalog, args.dataset_id)
    ensure_catalog_entry_ready(entry, connector)

    summary = build_and_store_multiscale_pyramid(
        settings=settings,
        connector=connector,
        entry=entry,
        overwrite=args.overwrite,
        zarr_format=args.zarr_format,
        chunk_size=args.chunk_size,
        min_size=args.min_size,
        max_browser_dimension=args.max_browser_dimension,
        full_resolution=args.full_resolution,
        output_dtype=args.dtype,
        prepopulate_through_zoom=args.prepopulate_through_zoom,
        prepopulate_tile_budget=args.prepopulate_tile_budget,
        max_zoom=args.max_zoom,
    )
    logger.info(
        "Multiscale generation complete for %s: output=%s levels=%s",
        entry.id,
        summary["output_store_path"],
        summary["levels"],
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


def _select_entry(catalog, dataset_id: str | None):
    if dataset_id:
        entry = catalog.get(dataset_id)
        if entry is None:
            raise SystemExit(f"Dataset id {dataset_id!r} was not found in the OCI catalog")
        return entry
    if len(catalog) != 1:
        available = ", ".join(sorted(catalog))
        raise SystemExit(f"Multiple datasets were indexed; pass --dataset-id. Available: {available}")
    return next(iter(catalog.values()))


def _assert_minimum_token_ttl(connector: OCIObjectStorageConnector, min_ttl_seconds: int) -> None:
    if min_ttl_seconds <= 0:
        return

    auth = connector.auth
    if auth.token_expires_at_epoch is None:
        return

    remaining = auth.token_expires_at_epoch - int(time.time())
    if remaining >= min_ttl_seconds:
        return

    raise SystemExit(
        "OCI CLI token expires too soon for this build "
        f"({remaining}s remaining, need at least {min_ttl_seconds}s). "
        "Re-authenticate and rerun, or lower --min-token-ttl-seconds if you want to override the guard."
    )


if __name__ == "__main__":
    raise SystemExit(main())
