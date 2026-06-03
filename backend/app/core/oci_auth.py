import configparser
import base64
import json
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
    auth_mode: str
    region_code: str | None
    token_expires_at_epoch: int | None = None


class OCIAuthExpiredError(RuntimeError):
    pass


def _load_local_oci_config(config_file: str, profile_name: str) -> dict:
    try:
        return oci.config.from_file(file_location=config_file, profile_name=profile_name)
    except oci.exceptions.InvalidConfig as error:
        parser = configparser.RawConfigParser()
        if not parser.read(config_file):
            raise
        if not parser.has_section(profile_name):
            raise

        profile = {key: value for key, value in parser.items(profile_name)}
        required_keys = ("fingerprint", "tenancy", "region", "key_file", "security_token_file")
        missing = [key for key in required_keys if not profile.get(key)]
        if missing:
            raise error
        if "missing" not in str(error).lower() or "user" not in str(error).lower():
            raise error

        profile["key_file"] = os.path.expanduser(profile["key_file"])
        profile["security_token_file"] = os.path.expanduser(profile["security_token_file"])
        return profile


def get_oci_auth_context(
    profile_name: str,
    config_file: str | None = None,
    auth_mode: str = "auto",
) -> OCIAuthContext:
    all_regions = {v: k for k, v in regions.REGIONS_SHORT_NAMES.items()}
    normalized_mode = (auth_mode or "auto").strip().lower().replace("-", "_")

    if normalized_mode not in {"auto", "security_token", "api_key", "resource_principal", "instance_principal"}:
        raise ValueError(
            "OCI_AUTH_MODE must be one of auto, security_token, api_key, "
            "resource_principal, or instance_principal"
        )

    if normalized_mode in {"auto", "resource_principal"} and in_dataflow():
        signer = signers.get_resource_principals_signer()
        region_name = os.environ.get("OCI_RESOURCE_PRINCIPAL_REGION", "")
        return OCIAuthContext(
            config={},
            signer=signer,
            auth_mode="resource_principal",
            region_code=all_regions.get(region_name),
        )

    if normalized_mode == "resource_principal":
        signer = signers.get_resource_principals_signer()
        region_name = os.environ.get("OCI_RESOURCE_PRINCIPAL_REGION", "")
        return OCIAuthContext(
            config={},
            signer=signer,
            auth_mode="resource_principal",
            region_code=all_regions.get(region_name),
        )

    if normalized_mode == "instance_principal":
        signer = signers.InstancePrincipalsSecurityTokenSigner()
        region_name = getattr(signer, "region", None)
        return OCIAuthContext(
            config={"region": region_name} if region_name else {},
            signer=signer,
            auth_mode="instance_principal",
            region_code=all_regions.get(region_name),
        )

    resolved_config_file = config_file or os.environ.get("OCI_CONFIG_FILE") or oci.config.DEFAULT_LOCATION
    oci_config = _load_local_oci_config(config_file=resolved_config_file, profile_name=profile_name)

    if normalized_mode == "api_key" or (normalized_mode == "auto" and not oci_config.get("security_token_file")):
        oci.config.validate_config(oci_config)
        return OCIAuthContext(
            config=oci_config,
            signer=oci.signer.Signer(
                tenancy=oci_config["tenancy"],
                user=oci_config["user"],
                fingerprint=oci_config["fingerprint"],
                private_key_file_location=os.path.expanduser(oci_config["key_file"]),
                pass_phrase=oci_config.get("pass_phrase"),
            ),
            auth_mode="api_key",
            region_code=all_regions.get(oci_config.get("region")),
        )

    if normalized_mode == "security_token" and not oci_config.get("security_token_file"):
        raise oci.exceptions.InvalidConfig("OCI security_token auth requires security_token_file in the profile")

    token_path = os.path.expanduser(oci_config["security_token_file"])
    with open(token_path, "r", encoding="utf-8") as handle:
        token = handle.read()
    token_expires_at_epoch = _extract_token_expiry_epoch(token)
    token_container = oci.auth.security_token_container.SecurityTokenContainer(None, token)
    if not token_container.valid():
        raise OCIAuthExpiredError(
            "OCI CLI token has expired. Re-authenticate before starting the backend."
        )
    private_key = oci.signer.load_private_key_from_file(oci_config["key_file"])
    signer = oci.auth.signers.SecurityTokenSigner(token, private_key)
    return OCIAuthContext(
        config=oci_config,
        signer=signer,
        auth_mode="security_token",
        region_code=all_regions.get(oci_config.get("region")),
        token_expires_at_epoch=token_expires_at_epoch,
    )


def _extract_token_expiry_epoch(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return None

    try:
        return int(parsed.get("exp"))
    except (TypeError, ValueError):
        return None
