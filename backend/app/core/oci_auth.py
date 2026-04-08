import os
from dataclasses import dataclass

import oci
from oci import regions
from oci.auth import signers


def in_dataflow() -> bool:
    return os.environ.get("HOME") == "/home/dataflow" or bool(
        os.environ.get("OCI_RESOURCE_PRINCIPAL_VERSION")
    )


@dataclass
class OCIAuthContext:
    config: dict
    signer: object
    region_code: str | None


def get_oci_auth_context(profile_name: str, config_file: str | None = None) -> OCIAuthContext:
    all_regions = {v: k for k, v in regions.REGIONS_SHORT_NAMES.items()}

    if in_dataflow():
        signer = signers.get_resource_principals_signer()
        region_name = os.environ.get("OCI_RESOURCE_PRINCIPAL_REGION", "")
        return OCIAuthContext(
            config={},
            signer=signer,
            region_code=all_regions.get(region_name),
        )

    resolved_config_file = config_file or os.environ.get("OCI_CONFIG_FILE") or oci.config.DEFAULT_LOCATION
    oci_config = oci.config.from_file(file_location=resolved_config_file, profile_name=profile_name)
    token_path = os.path.expanduser(oci_config["security_token_file"])
    with open(token_path, "r", encoding="utf-8") as handle:
        token = handle.read()
    token_container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
    if not token_container.valid():
        raise RuntimeError(
            "OCI CLI token has expired. Re-authenticate before starting the backend."
        )
    private_key = oci.signer.load_private_key_from_file(oci_config["key_file"])
    signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
    return OCIAuthContext(
        config=oci_config,
        signer=signer,
        region_code=all_regions.get(oci_config.get("region")),
    )
