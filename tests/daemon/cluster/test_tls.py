"""The cluster's credentials: one secret, the same CA on every node, a leaf per node naming it, and the previous
secret accepted while a new one rolls out."""

from __future__ import annotations

import datetime as dt
import os
import ssl
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from rrotord.cluster import tls


def test_every_node_derives_the_same_ca_from_the_secret_and_another_secret_another():
    secret = os.urandom(32)
    one, two = tls.ca_certificate(tls.ca_key(secret)), tls.ca_certificate(tls.ca_key(secret))
    der = serialization.Encoding.DER
    assert one.public_bytes(der) == two.public_bytes(der)
    other = tls.ca_certificate(tls.ca_key(os.urandom(32)))
    assert other.public_bytes(der) != one.public_bytes(der)
    assert one.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_a_leaf_names_its_node_and_is_signed_by_the_ca(tmp_path: Path):
    key = tls.ca_key(os.urandom(32))
    ca = tls.ca_certificate(key)
    cert_pem, key_pem = tls.leaf(key, ca, "rrotor-1", dt.datetime.now(dt.UTC))
    cert = x509.load_pem_x509_certificate(cert_pem)
    cert.verify_directly_issued_by(ca)
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == ["rrotor-1"] and b"PRIVATE KEY" in key_pem
    made = tls.Credentials([os.urandom(32), os.urandom(32)], "rrotor-1", tmp_path / "c")
    assert len(made.identities) == 2 and made.server is made.identities[0].server
    assert list((tmp_path / "c").iterdir()) == []  # the keys are loaded, never left on disk
    assert made.identities[1].server_name.endswith(tls.SUFFIX)


class FakeSsl:
    def __init__(self, der: bytes | None) -> None:
        self.der = der

    def getpeercert(self, binary_form: bool = False) -> Any:
        return self.der


def cert_with(names: list[str] | None) -> bytes:
    key = Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        .not_valid_after(dt.datetime(2027, 1, 1, tzinfo=dt.UTC))
    )
    if names is not None:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
    return builder.sign(key, None).public_bytes(serialization.Encoding.DER)


def test_the_peers_name_is_its_certificates_one_dns_name():
    assert tls.peer_name(None) is None
    assert tls.peer_name(FakeSsl(None)) is None  # type: ignore[arg-type]
    assert tls.peer_name(FakeSsl(cert_with(None))) is None  # type: ignore[arg-type]
    assert tls.peer_name(FakeSsl(cert_with(["a", "b"]))) is None  # type: ignore[arg-type]
    assert tls.peer_name(FakeSsl(cert_with(["rrotor-2"]))) == "rrotor-2"  # type: ignore[arg-type]


def test_the_listener_presents_the_leaf_the_dialers_server_name_asks_for(tmp_path: Path):
    made = tls.Credentials([os.urandom(32), os.urandom(32)], "n", tmp_path)
    pick = made.server.sni_callback
    assert pick is not None

    class Sock:
        context: ssl.SSLContext = made.server

    sock = Sock()
    pick(sock, made.identities[1].server_name, made.server)  # type: ignore[arg-type]
    assert sock.context is made.identities[1].server
    sock.context = made.server
    pick(sock, None, made.server)  # type: ignore[arg-type]
    assert sock.context is made.server
