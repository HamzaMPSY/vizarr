# VM Handoff

This project can be moved to another VM as a source bundle.

The bundle intentionally excludes:

- `.git`
- local virtual environments
- `node_modules`
- local caches
- local `.env`

## 1. Create The Zip On The Current Machine

From the repo root:

```bash
chmod +x scripts/make_vm_bundle.sh
./scripts/make_vm_bundle.sh
```

Custom output path:

```bash
./scripts/make_vm_bundle.sh /tmp/vizarr-vm-bundle.zip
```

## 2. Copy The Zip To The VM

Example:

```bash
scp /tmp/vizarr-vm-bundle.zip <user>@<vm-host>:/tmp/
```

Or if you used the default output path:

```bash
scp vizarr-vm-bundle.zip <user>@<vm-host>:/tmp/
```

## 3. Unzip On The VM

```bash
mkdir -p ~/work
cd ~/work
unzip /tmp/vizarr-vm-bundle.zip -d vizarr
cd vizarr
```

If you copied the default-named zip into another path, adjust the input path accordingly.

## 4. VM Prerequisites

Install these on the VM:

- Python 3.12+
- `uv`
- Node.js 20+ and `npm`
- OCI CLI if you want OCI session auth on the VM
- `unzip`

Quick checks:

```bash
python3 --version
uv --version
node --version
npm --version
oci --version
```

## 5. Backend Setup On The VM

```bash
cd ~/work/vizarr/backend
uv venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 6. Frontend Setup On The VM

```bash
cd ~/work/vizarr/frontend
npm install
```

If your VM needs the Oracle npm registry, create the root `.env` file from the original machine values before running `npm install`.

Example root `.env`:

```text
NPM_CONFIG_REGISTRY=https://artifactory.oci.oraclecorp.com/api/npm/global-dev-npm/
NO_PROXY=localhost,127.0.0.1,backend,frontend,redis,nginx
NPM_CONFIG_STRICT_SSL=true
NODE_TLS_REJECT_UNAUTHORIZED=1
```

## 7. OCI Auth On The VM

The live maize flow expects OCI local profile auth.

Put your OCI config on the VM at:

```bash
~/.oci/config
```

Then authenticate or refresh the session on the VM:

```bash
oci session authenticate --profile-name prof
```

If your OCI setup already exists, just confirm the token is valid.

## 8. Run The Live Backend On The VM

This uses an OCI Zarr prefix that you provide on the VM:

```bash
cd ~/work/vizarr/backend

OCI_CONFIG_FILE=$HOME/.oci/config \
OCI_CONFIG_PROFILE=prof \
STORAGE_BACKEND=oci_zarr \
OCI_NAMESPACE=<object-storage-namespace> \
OCI_BUCKET=<bucket-name> \
OCI_PREFIX=<prefix-or-zarr-store> \
OCI_BROWSE_PREFIX_ROOT=browse \
BROWSE_PREWARM_ENABLED=false \
PYTHONPATH=$PWD \
uv run --python .venv/bin/python --no-project uvicorn app.main:app --host 0.0.0.0 --port 8015
```

## 9. Run The Frontend On The VM

In another shell:

```bash
cd ~/work/vizarr/frontend
VITE_PROXY_TARGET=http://127.0.0.1:8015 npm run dev -- --host 0.0.0.0 --port 5173
```

## 10. Quick Verification On The VM

Backend health:

```bash
curl http://127.0.0.1:8015/api/healthz
```

Datasets:

```bash
curl http://127.0.0.1:8015/api/datasets
```

Variables, after copying a real dataset id from the datasets response:

```bash
curl http://127.0.0.1:8015/api/datasets/<dataset-id>/variables
```

Frontend:

```text
http://<vm-host>:5173
```

## 11. Important Notes

- The zip does not include secrets or live OCI session tokens.
- The zip does not include `backend/.venv` or `frontend/node_modules`; they are recreated on the VM.
- Low-zoom seamless performance still depends on browse artifacts being present in OCI.
- If the OCI token expires on the VM, refresh it there and restart the backend.
