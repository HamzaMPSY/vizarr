oci://Ayoub@lrdwfp6kyp5x/maize_2025-01-08_to_2025-01-15.parquet


printf '%s\n' \
    'oci://Ayoub@lrdwfp6kyp5x/maize_2025-01-08_to_2025-01-15.parquet' \
    'oci://Ayoub@lrdwfp6kyp5x/maize_2025-01-15_to_2025-01-22.parquet' \
    'oci://Ayoub@lrdwfp6kyp5x/maize_2025-01-22_to_2025-01-29.parquet' \
    > /tmp/maize_links.txt

uv run --python .venv/bin/python --no-project python -m app.tools.parquet_to_zarr \
--links-file /tmp/maize_links.txt \
--output-store oci://Ayoub@lrdwfp6kyp5x/cubes/maize_2025.zarr \
--x-column LONGITUDE \
--y-column LATITUDE \
--value-columns NDVI \
--timestamp-column START_DATE \
--layout bands \
--zarr-version 3 \
--chunk-size 256 \
--shard-size 4096 \
--crs EPSG:4326 \
--source-crs EPSG:4326 \
--x-resolution 0.0001 \
--y-resolution 0.0001 \
--preserve-points \
--overwrite \
--log-level DEBUG
