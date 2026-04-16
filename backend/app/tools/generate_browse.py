from __future__ import annotations

import argparse
import json
import logging

from app.config import get_settings
from app.core.browse_tiles import build_and_store_browse_overviews
from app.core.dataset_catalog import build_catalog_index
from app.core.dataset_catalog import ensure_catalog_entry_ready
from app.core.oci_object_storage import OCIObjectStorageConnector


logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate durable OCI-backed browse overview artifacts for indexed Zarr datasets."
    )
    parser.add_argument(
        "--dataset-id",
        help="Optional encoded dataset id. If omitted and only one dataset is indexed, that dataset is used.",
    )
    parser.add_argument(
        "--variable",
        action="append",
        dest="variables",
        help="Variable id to generate. Repeat to generate more than one variable. Defaults to all dataset variables.",
    )
    parser.add_argument(
        "--time-index",
        action="append",
        dest="time_indices",
        type=int,
        help="Time index to generate. Repeat to generate more than one. Default: 0.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate and replace existing browse overview objects.",
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
        raise SystemExit("Browse generation requires STORAGE_BACKEND=oci_zarr")

    connector = OCIObjectStorageConnector(settings)
    catalog = build_catalog_index(settings, connector)
    if not catalog:
        raise SystemExit("No OCI Zarr datasets were indexed for browse generation")

    entry = _select_entry(catalog, args.dataset_id)
    ensure_catalog_entry_ready(entry, connector)

    variables = args.variables or [item.id for item in entry.meta.variables]
    time_indices = args.time_indices or [0]
    summary = build_and_store_browse_overviews(
        settings=settings,
        connector=connector,
        entry=entry,
        variables=variables,
        time_indices=time_indices,
        overwrite=args.overwrite,
    )
    logger.info(
        "Browse generation complete for %s: generated=%s reused=%s manifest=%s",
        entry.id,
        summary["generated"],
        summary["reused"],
        summary["manifest_path"],
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


if __name__ == "__main__":
    raise SystemExit(main())
