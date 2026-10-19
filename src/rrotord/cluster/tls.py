"""The cluster's credentials (docs/specs/cluster.md, 6): one secret, mutual TLS from it.

`RROTOR_CLUSTER_SECRET` (32 bytes or more, base64) is the only thing the operator makes. From it every node derives,
with HKDF-SHA256, the same Ed25519 **cluster CA** key and the same CA certificate (Ed25519 signs deterministically
and every field is fixed, so the certificate is byte for byte the same on every node). At start a node makes its own
key and issues itself a leaf certificate from that CA, its node id the one name in its SAN. Both ends of every
connection require a certificate from the cluster CA (TLS 1.3, `CERT_REQUIRED` on both sides); the node id in the
peer's certificate is the one its `hello` must name.

While a new secret rolls out, `RROTOR_CLUSTER_SECRET_PREVIOUS` is accepted too: a node trusts both CAs, holds a leaf
from each, and offers the previous one where the current one is refused. The server side picks the leaf by the TLS
server name the dialer sends, which names the CA (`<tag>.rrotor-cluster`, the tag the CA certificate's SHA-256
prefix), so a node not yet rolled and one already rolled still meet.

Never a runtime key: those are callers' credentials; nothing here touches them."""

from __future__ import annotations

import datetime as dt
import hashlib
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

SALT = b"rrotor cluster"
CA_INFO = b"rrotor cluster ca v1"
CA_NAME = "rrotor cluster CA"
CA_FROM = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)  # fixed: the CA certificate is the same on every node
CA_UNTIL = dt.datetime(2126, 1, 1, tzinfo=dt.UTC)
LEAF_YEARS = 10  # a leaf is made at every start; this only has to outlive the process
SUFFIX = ".rrotor-cluster"
COMMAND = "command" + SUFFIX  # the SAN of `rrotord cluster ...`'s own leaf: never a node id (they have no dot)


def ca_key(secret: bytes) -> Ed25519PrivateKey:
    seed = HKDF(algorithm=hashes.SHA256(), length=32, salt=SALT, info=CA_INFO).derive(secret)
    return Ed25519PrivateKey.from_private_bytes(seed)


def ca_certificate(key: Ed25519PrivateKey) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    public = key.public_key()
    serial = int.from_bytes(hashlib.sha256(public.public_bytes_raw()).digest()[:16], "big") >> 1
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(serial)
        .not_valid_before(CA_FROM)
        .not_valid_after(CA_UNTIL)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, None)
    )


def leaf(ca: Ed25519PrivateKey, ca_cert: x509.Certificate, name: str, now: dt.datetime) -> tuple[bytes, bytes]:
    """A new key and a certificate for it from the CA, `name` its one SAN: (certificate PEM, key PEM)."""
    key = Ed25519PrivateKey.generate()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=365 * LEAF_YEARS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH, x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),
            critical=False,
        )
        .sign(ca, None)
    )
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM), pem


@dataclass(frozen=True, slots=True)
class Identity:
    """One secret's CA and this node's leaf from it, as the TLS contexts that present it."""

    tag: str  # the CA certificate's SHA-256, first 16 hex: what the dialer's server name asks for
    ca_pem: str
    server: ssl.SSLContext
    client: ssl.SSLContext

    @property
    def server_name(self) -> str:
        return self.tag + SUFFIX


def context(purpose: ssl.Purpose, chain: Path, trusted: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if purpose is ssl.Purpose.CLIENT_AUTH else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False  # the server name names the CA; the peer's node id is checked against its hello
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=trusted)
    ctx.load_cert_chain(chain)
    return ctx


class Credentials:
    """This node's TLS: the CAs it trusts (the secret's and, while one rolls out, the previous one's) and its leaves."""

    def __init__(self, secrets: list[bytes], name: str, folder: Path, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.UTC)
        cas = [(key, ca_certificate(key)) for key in (ca_key(s) for s in secrets)]
        trusted = "".join(cert.public_bytes(serialization.Encoding.PEM).decode() for _, cert in cas)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.identities: list[Identity] = []
        for key, cert in cas:
            tag = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()[:16]
            chain = folder / f"{name}-{tag}.pem"
            cert_pem, key_pem = leaf(key, cert, name, now)
            chain.touch(mode=0o600)
            chain.write_bytes(cert_pem + key_pem)
            server = context(ssl.Purpose.CLIENT_AUTH, chain, trusted)
            client = context(ssl.Purpose.SERVER_AUTH, chain, trusted)
            chain.unlink()  # loaded: the key stays in memory only
            self.identities.append(
                Identity(tag, cert.public_bytes(serialization.Encoding.PEM).decode(), server, client)
            )
        by_name = {i.server_name: i.server for i in self.identities}

        def pick(sock: ssl.SSLObject, server_name: str | None, _: ssl.SSLContext) -> None:
            chosen = by_name.get(server_name or "")
            if chosen is not None:
                sock.context = chosen

        self.identities[0].server.sni_callback = pick

    @property
    def server(self) -> ssl.SSLContext:
        """The listener's context: the current leaf, or the previous one where the dialer asks for it."""
        return self.identities[0].server


def peer_name(ssl_object: ssl.SSLObject | ssl.SSLSocket | None) -> str | None:
    """The one DNS name in the verified peer certificate's SAN (a node id, or COMMAND)."""
    if ssl_object is None:
        return None
    der = ssl_object.getpeercert(binary_form=True)
    if not der:
        return None
    cert = x509.load_der_x509_certificate(der)
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.DNSName
        )
    except x509.ExtensionNotFound:
        return None
    return names[0] if len(names) == 1 else None
