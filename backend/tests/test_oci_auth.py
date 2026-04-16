from pathlib import Path

from app.core.oci_auth import get_oci_auth_context


def test_get_oci_auth_context_accepts_security_token_profile_without_user(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config"
    key_path = tmp_path / "session.pem"
    token_path = tmp_path / "token"
    key_path.write_text("private-key", encoding="utf-8")
    token_path.write_text("token", encoding="utf-8")
    config_path.write_text(
        "\n".join(
            [
                "[prof]",
                "fingerprint=fingerprint",
                "tenancy=ocid1.tenancy.oc1..example",
                "region=us-ashburn-1",
                f"key_file={key_path}",
                f"security_token_file={token_path}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    class FakeTokenContainer:
        def __init__(self, _unused, token: str) -> None:
            self.token = token

        def valid(self) -> bool:
            return True

    monkeypatch.setattr(
        "app.core.oci_auth.oci.auth.security_token_container.SecurityTokenContainer",
        FakeTokenContainer,
    )
    monkeypatch.setattr(
        "app.core.oci_auth.oci.signer.load_private_key_from_file",
        lambda _path: "private-key",
    )
    monkeypatch.setattr(
        "app.core.oci_auth.oci.auth.signers.SecurityTokenSigner",
        lambda token, private_key: {
            "token": token,
            "private_key": private_key,
        },
    )

    auth = get_oci_auth_context(profile_name="prof", config_file=str(config_path))

    assert auth.config["region"] == "us-ashburn-1"
    assert auth.config["tenancy"] == "ocid1.tenancy.oc1..example"
    assert auth.config["key_file"] == str(key_path)
    assert auth.config["security_token_file"] == str(token_path)
    assert auth.signer == {"token": "token", "private_key": "private-key"}
