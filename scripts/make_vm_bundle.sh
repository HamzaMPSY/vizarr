#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-$ROOT_DIR/vizarr-vm-bundle.zip}"

mkdir -p "$(dirname "$OUTPUT_PATH")"

cd "$ROOT_DIR"
rm -f "$OUTPUT_PATH"

ZIP_ARGS=(
  -r "$OUTPUT_PATH" .
  -x ".git/*"
  -x ".DS_Store"
  -x ".cache/*"
  -x ".env"
  -x "backend/.venv/*"
  -x "backend/.cache/*"
  -x "backend/.pytest_cache/*"
  -x "backend/__pycache__/*"
  -x "backend/**/*.pyc"
  -x "frontend/node_modules/*"
  -x "frontend/dist/*"
  -x "__pycache__/*"
  -x "**/__pycache__/*"
  -x "*.pyc"
)

if [[ "$OUTPUT_PATH" == "$ROOT_DIR"/* ]]; then
  ZIP_ARGS+=(-x "${OUTPUT_PATH#$ROOT_DIR/}")
fi

zip "${ZIP_ARGS[@]}"

echo "Created VM bundle: $OUTPUT_PATH"
