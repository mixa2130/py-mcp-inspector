"""Shared fixtures, including a throwaway CA and certificates for the TLS tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CA_CONFIG = """\
[req]
distinguished_name=dn
x509_extensions=v3_ca
prompt=no
[dn]
CN=PyMCPinspector Test CA
[v3_ca]
basicConstraints=critical,CA:TRUE
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
"""

LEAF_EXT = """\
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage={usage}
{san}
"""

KEY_PASSPHRASE = "hunter2"


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture(scope="session")
def certs(tmp_path_factory) -> dict[str, Path]:
    """A CA plus a server and client certificate, signed by it.

    Skipped rather than failed when `openssl` is unavailable: the rest of the
    suite does not need it.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not on PATH")

    out = tmp_path_factory.mktemp("certs")
    (out / "ca.cnf").write_text(CA_CONFIG)
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
             "-keyout", str(out / "ca.key"), "-out", str(out / "ca.pem"),
             "-config", str(out / "ca.cnf"))

    for name, usage, san in (
        ("server", "serverAuth", "subjectAltName=DNS:localhost,IP:127.0.0.1"),
        ("client", "clientAuth", ""),
    ):
        (out / f"{name}.ext").write_text(LEAF_EXT.format(usage=usage, san=san))
        _openssl("req", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(out / f"{name}.key"), "-out", str(out / f"{name}.csr"),
                 "-subj", f"/CN={'localhost' if name == 'server' else 'inspector-client'}")
        _openssl("x509", "-req", "-days", "2", "-in", str(out / f"{name}.csr"),
                 "-CA", str(out / "ca.pem"), "-CAkey", str(out / "ca.key"), "-CAcreateserial",
                 "-out", str(out / f"{name}.pem"), "-extfile", str(out / f"{name}.ext"))

    _openssl("rsa", "-in", str(out / "client.key"), "-aes256",
             "-passout", f"pass:{KEY_PASSPHRASE}", "-out", str(out / "client-enc.key"))
    (out / "client-bundle.pem").write_text(
        (out / "client.pem").read_text() + (out / "client.key").read_text()
    )

    ca_dir = out / "cadir"
    ca_dir.mkdir()
    shutil.copy(out / "ca.pem", ca_dir / "ca.pem")
    subject_hash = subprocess.run(
        ["openssl", "x509", "-hash", "-noout", "-in", str(out / "ca.pem")],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (ca_dir / f"{subject_hash}.0").symlink_to("ca.pem")

    return {
        "passphrase": KEY_PASSPHRASE,
        "ca": out / "ca.pem",
        "ca_dir": ca_dir,
        "server_cert": out / "server.pem",
        "server_key": out / "server.key",
        "client_cert": out / "client.pem",
        "client_key": out / "client.key",
        "client_key_encrypted": out / "client-enc.key",
        "client_bundle": out / "client-bundle.pem",
    }
