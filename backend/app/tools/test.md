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


podman exec vizarr_backend_1 python -m app.tools.parquet_to_zarr \
--source-bucket bu-lhr-dp-dibe-si007-dev-detcd-AppintegDIdev \
--parquet-prefix '20260401/48C676A9D6277B3A21F4EC87F8C70F56B7D057CE3AF4E2CDF79E11221840C639/F5347356860C76BC6E7A6B9505789C79798191668A626078CC704568E5294423/20260325_20260401/1d2053b2-34b7-49b1-8b8f-0935d4bf1b0b/35MQS_1_0_2026-03-25_2026-04-01.parquet' \
--output-store cubes/35MQS_1_0_2026-03-25_2026-04-01.zarr \
--layout bands \
--crs EPSG:4326 \
--source-crs EPSG:4326 \
--overwrite \
--log-level INFO
