"""TLS: CA selection, client certificates, and a real mutual-TLS connection."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from pymcpinspector.app import create_app
from pymcpinspector.models import ConnectionConfig
from pymcpinspector.store import ServerStore
from pymcpinspector.transports import build_ssl_context

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "demo_server.py"


def config(**kwargs) -> ConnectionConfig:
    return ConnectionConfig(transport="streamable-http", url="https://example.invalid/mcp", **kwargs)


# ----------------------------------------------------------------- contexts


def test_untouched_tls_keeps_httpx_defaults():
    assert build_ssl_context(config()) is True


def test_verification_can_be_switched_off():
    import ssl

    context = build_ssl_context(config(verify_tls=False))
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


def test_ca_file_and_directory_both_load(certs):
    assert build_ssl_context(config(ca_bundle=str(certs["ca"]))).get_ca_certs()
    assert build_ssl_context(config(ca_bundle=str(certs["ca_dir"]))) is not None


def test_client_certificate_variants_load(certs):
    assert build_ssl_context(
        config(ca_bundle=str(certs["ca"]), client_cert=str(certs["client_cert"]), client_key=str(certs["client_key"]))
    )
    assert build_ssl_context(
        config(
            ca_bundle=str(certs["ca"]),
            client_cert=str(certs["client_cert"]),
            client_key=str(certs["client_key_encrypted"]),
            client_key_password=certs["passphrase"],
        )
    )
    # Certificate and key concatenated into one file: no separate key needed.
    assert build_ssl_context(config(ca_bundle=str(certs["ca"]), client_cert=str(certs["client_bundle"])))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ca_bundle": "/nope/ca.pem"}, "CA bundle not found"),
        ({"client_cert": "/nope/c.pem"}, "Client certificate not found"),
        ({"client_key": "/nope/k.pem"}, "without a client certificate"),
    ],
)
def test_missing_paths_are_reported_before_connecting(kwargs, message):
    with pytest.raises(ValueError, match=message):
        build_ssl_context(config(**kwargs))


def test_encrypted_key_without_passphrase_fails_instead_of_prompting(certs):
    """OpenSSL would otherwise block on a terminal prompt and hang the server."""
    with pytest.raises(ValueError, match="if the key is encrypted"):
        build_ssl_context(
            config(client_cert=str(certs["client_cert"]), client_key=str(certs["client_key_encrypted"]))
        )


def test_wrong_passphrase_says_so(certs):
    with pytest.raises(ValueError, match="check the passphrase"):
        build_ssl_context(
            config(
                client_cert=str(certs["client_cert"]),
                client_key=str(certs["client_key_encrypted"]),
                client_key_password="wrong",
            )
        )


def test_summary_never_leaks_the_passphrase():
    summary = config(client_cert="/c.pem", client_key_password="s3cret").tls_summary()
    assert summary["client_key_encrypted"] is True
    assert "s3cret" not in str(summary)


# ---------------------------------------------------------------------- e2e


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mtls_server(certs):
    """The demo server over HTTPS, demanding a client certificate."""
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable, str(DEMO),
            "--transport", "streamable-http", "--port", str(port),
            "--ssl-certfile", str(certs["server_cert"]),
            "--ssl-keyfile", str(certs["server_key"]),
            "--ssl-ca-certs", str(certs["ca"]),
            "--require-client-cert",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"demo server exited: {(process.stdout.read() or b'').decode()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("demo server did not start")

    yield f"https://127.0.0.1:{port}/mcp"
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
async def tls_client(tmp_path):
    app = create_app(ServerStore(tmp_path / "servers.json"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://inspector"
    ) as client:
        yield client


async def _connect(client, url, **tls):
    return (
        await client.post(
            "/api/connect",
            json={"transport": "streamable-http", "url": url, **tls},
            timeout=30,
        )
    ).json()


async def test_private_ca_is_not_trusted_by_default(tls_client, mtls_server):
    body = await _connect(tls_client, mtls_server)
    assert body["error"]["kind"] == "connect"


async def test_client_certificate_is_required_by_this_server(tls_client, mtls_server, certs):
    body = await _connect(tls_client, mtls_server, ca_bundle=str(certs["ca"]))
    assert body.get("error"), "the server should refuse a connection with no client certificate"


async def test_mutual_tls_connects_and_calls_a_tool(tls_client, mtls_server, certs):
    body = await _connect(
        tls_client,
        mtls_server,
        ca_bundle=str(certs["ca"]),
        client_cert=str(certs["client_cert"]),
        client_key=str(certs["client_key"]),
    )
    assert body.get("status") == "connected", body
    assert body["tls"]["ca_bundle"] == str(certs["ca"])

    called = (
        await tls_client.post("/api/tools/call", json={"name": "add", "arguments": {"a": 19, "b": 23}})
    ).json()
    assert called["result"]["structuredContent"] == {"result": 42.0}
    await tls_client.post("/api/disconnect", json={}, timeout=30)


async def test_encrypted_client_key_connects_with_its_passphrase(tls_client, mtls_server, certs):
    body = await _connect(
        tls_client,
        mtls_server,
        ca_bundle=str(certs["ca"]),
        client_cert=str(certs["client_cert"]),
        client_key=str(certs["client_key_encrypted"]),
        client_key_password=certs["passphrase"],
    )
    assert body.get("status") == "connected", body
    assert body["tls"]["client_key_encrypted"] is True
    await tls_client.post("/api/disconnect", json={}, timeout=30)


async def test_verification_off_still_sends_the_client_certificate(tls_client, mtls_server, certs):
    body = await _connect(
        tls_client, mtls_server, verify_tls=False, client_cert=str(certs["client_bundle"])
    )
    assert body.get("status") == "connected", body
    await tls_client.post("/api/disconnect", json={}, timeout=30)
