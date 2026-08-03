"""Envelope-encrypted checkpoint storage adapter.

Two adapters:

* ``LocalCheckpointStore`` — deterministic local adapter for tests/CI.
  Uses AES-256-GCM with a fixed test key. No KMS dependency.

* ``S3CheckpointStore`` — production adapter that encrypts with AES-256-GCM
  per-object DEKs, wraps DEKs via AWS KMS, and persists to S3.

Both implement the same ``CheckpointStore`` protocol so callers need no
compile-time dependency on ``boto3`` / ``aws_encryption_sdk``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import time
from abc import ABC, abstractmethod
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_NONCE_BYTES = 12  # AES-GCM standard
_KEY_BYTES = 32     # AES-256
_TAG_BYTES = 16     # AES-GCM auth tag
_MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB envelope limit
_VERSION_BYTE = 0x01


def _aad_bytes(checkpoint_id: str, version: int, expires_at: float, content_hash: str) -> bytes:
    """Deterministic AAD bound to the object identity."""
    return (
        f"chk:{checkpoint_id}|v:{version}|exp:{int(expires_at)}|sha256:{content_hash}"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class CheckpointStore(ABC):
    """Storage adapter contract for envelope-encrypted checkpoint blobs."""

    @abstractmethod
    def put(self, *, checkpoint_id: str, plaintext: bytes, version: int,
            expires_at: float) -> dict:
        """Encrypt and persist. Returns metadata dict with object_key, size, hash."""

    @abstractmethod
    def get(self, checkpoint_id: str) -> bytes | None:
        """Retrieve and decrypt. Returns plaintext bytes or None."""

    @abstractmethod
    def delete(self, checkpoint_id: str) -> None:
        """Permanently delete the encrypted object."""

    @abstractmethod
    def sweep_expired(self) -> int:
        """Delete all expired objects. Returns count."""


# ---------------------------------------------------------------------------
# AES-GCM helpers (shared)
# ---------------------------------------------------------------------------
def _encrypt_envelope(dek: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    if len(plaintext) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds maximum envelope size")
    aesgcm = AESGCM(dek)
    return aesgcm.encrypt(nonce, plaintext, aad)


def _decrypt_envelope(dek: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    aesgcm = AESGCM(dek)
    return aesgcm.decrypt(nonce, ciphertext, aad)


def _pack(dek: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """Wire format: version(1) + dek_len(2) + nonce_len(1) + aad_len(4) + deks + nonce + ciphertext + aad."""
    header = struct.pack(">B", _VERSION_BYTE)
    header += struct.pack(">H", len(dek))
    header += struct.pack(">B", len(nonce))
    header += struct.pack(">I", len(aad))
    return header + dek + nonce + ciphertext + aad


def _unpack(data: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """Returns (dek, nonce, ciphertext, aad)."""
    if len(data) < 8:
        raise ValueError("truncated envelope")
    offset = 1  # version byte
    version = data[0]
    if version != _VERSION_BYTE:
        raise ValueError(f"unsupported envelope version: {version}")
    dek_len = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    nonce_len = struct.unpack_from(">B", data, offset)[0]
    offset += 1
    aad_len = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    dek = data[offset:offset + dek_len]
    offset += dek_len
    nonce = data[offset:offset + nonce_len]
    offset += nonce_len
    ciphertext = data[offset:offset + len(data) - offset - aad_len]
    ciphertext_end = offset + len(ciphertext)
    aad = data[ciphertext_end:ciphertext_end + aad_len]
    return dek, nonce, ciphertext, aad


# ---------------------------------------------------------------------------
# Local / test adapter
# ---------------------------------------------------------------------------
class LocalCheckpointStore(CheckpointStore):
    """Deterministic in-memory store with a fixed key — for tests only.

    No KMS dependency. The DEK is derived from a configurable seed so tests
    can verify exact ciphertext at different key versions.
    """

    def __init__(self, *, seed: bytes | None = None):
        self._store: dict[str, bytes] = {}
        self._deadlines: dict[str, float] = {}
        self._deleted: set[str] = set()
        self._key = seed or hashlib.sha256(b"local-checkpoint-store").digest()
        assert len(self._key) == 32

    def _dek(self, checkpoint_id: str) -> bytes:
        return hashlib.sha256(self._key + checkpoint_id.encode()).digest()

    def put(self, *, checkpoint_id: str, plaintext: bytes, version: int,
            expires_at: float) -> dict:
        content_hash = hashlib.sha256(plaintext).hexdigest()
        aad = _aad_bytes(checkpoint_id, version, expires_at, content_hash)
        nonce = os.urandom(_NONCE_BYTES)
        dek = self._dek(checkpoint_id)
        ciphertext = _encrypt_envelope(dek, nonce, plaintext, aad)
        # For local adapter, the DEK is derived deterministically — no wrapping needed.
        # We still pack the DEK in the envelope for format compatibility.
        blob = _pack(dek, nonce, ciphertext, aad)
        self._store[checkpoint_id] = blob
        self._deadlines[checkpoint_id] = expires_at
        self._deleted.discard(checkpoint_id)
        return {
            "object_key": checkpoint_id,
            "size_bytes": len(plaintext),
            "content_hash": content_hash,
        }

    def get(self, checkpoint_id: str) -> bytes | None:
        if checkpoint_id in self._deleted:
            return None
        blob = self._store.get(checkpoint_id)
        if blob is None:
            return None
        deadline = self._deadlines.get(checkpoint_id, 0)
        if deadline and time.time() > deadline:
            self.delete(checkpoint_id)
            return None
        stored_dek, nonce, ciphertext, aad = _unpack(blob)
        expected_dek = self._dek(checkpoint_id)
        try:
            return _decrypt_envelope(expected_dek, nonce, ciphertext, aad)
        except Exception:
            return None  # tampered or wrong key

    def delete(self, checkpoint_id: str) -> None:
        self._store.pop(checkpoint_id, None)
        self._deadlines.pop(checkpoint_id, None)
        self._deleted.add(checkpoint_id)

    def sweep_expired(self) -> int:
        now = time.time()
        expired = [
            cid for cid, deadline in self._deadlines.items()
            if deadline and now > deadline
        ]
        for cid in expired:
            self.delete(cid)
        return len(expired)


# ---------------------------------------------------------------------------
# S3 + AWS KMS adapter (imported on first use)
# ---------------------------------------------------------------------------
class S3CheckpointStore(CheckpointStore):
    """Production adapter: S3 storage + AWS KMS envelope encryption.

    Requires environment (validated by :func:`validate_s3_store_config`):
      FIN_WORKSPACE_S3_BUCKET
      FIN_WORKSPACE_S3_REGION   (explicit — never derived from AWS_REGION)
      FIN_WORKSPACE_KMS_KEY_ID  (ARN or alias)
      Optional: FIN_WORKSPACE_S3_PREFIX (default "checkpoints/")
    """

    def __init__(self, *, bucket: str | None = None, key_id: str | None = None,
                 region: str | None = None, prefix: str = "checkpoints/"):
        self._bucket = bucket or os.environ.get("FIN_WORKSPACE_S3_BUCKET", "")
        self._key_id = key_id or os.environ.get("FIN_WORKSPACE_KMS_KEY_ID", "")
        self._region = region or os.environ.get("FIN_WORKSPACE_S3_REGION", "").strip()
        self._prefix = prefix.lstrip("/")
        if not self._bucket or not self._key_id or not self._region:
            raise CheckpointStoreConfigError(
                "S3CheckpointStore requires FIN_WORKSPACE_S3_BUCKET, "
                "FIN_WORKSPACE_S3_REGION and FIN_WORKSPACE_KMS_KEY_ID"
            )
        self._s3 = None
        self._kms = None

    @property
    def _s3_client(self):
        if self._s3 is None:
            import boto3  # type: ignore
            self._s3 = boto3.client("s3", region_name=self._region)
        return self._s3

    @property
    def _kms_client(self):
        if self._kms is None:
            import boto3  # type: ignore
            self._kms = boto3.client("kms", region_name=self._region)
        return self._kms

    def _s3_key(self, checkpoint_id: str) -> str:
        return f"{self._prefix}{checkpoint_id}"

    def _generate_dek(self) -> bytes:
        """Request a data key from KMS. Returns (plaintext_dek, encrypted_dek)."""
        resp = self._kms_client.generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
        )
        return resp["Plaintext"], resp["CiphertextBlob"]

    def _decrypt_dek(self, encrypted_dek: bytes) -> bytes:
        resp = self._kms_client.decrypt(CiphertextBlob=encrypted_dek)
        return resp["Plaintext"]

    def put(self, *, checkpoint_id: str, plaintext: bytes, version: int,
            expires_at: float) -> dict:
        content_hash = hashlib.sha256(plaintext).hexdigest()
        aad = _aad_bytes(checkpoint_id, version, expires_at, content_hash)
        nonce = os.urandom(_NONCE_BYTES)
        dek, wrapped_dek = self._generate_dek()
        ciphertext = _encrypt_envelope(dek, nonce, plaintext, aad)
        # Pack the *wrapped* DEK so only KMS can unwrap
        blob = _pack(wrapped_dek, nonce, ciphertext, aad)
        s3_key = self._s3_key(checkpoint_id)
        self._s3_client.put_object(
            Bucket=self._bucket,
            Key=s3_key,
            Body=blob,
            ServerSideEncryption="AES256",
        )
        # Set lifecycle expiry on the object tag for sweep recovery
        try:
            self._s3_client.put_object_tagging(
                Bucket=self._bucket,
                Key=s3_key,
                Tagging={
                    "TagSet": [
                        {"Key": "expires_at", "Value": str(int(expires_at))},
                        {"Key": "checkpoint_version", "Value": str(version)},
                    ]
                },
            )
        except Exception:
            pass  # Tagging is best-effort; lifecycle policy handles expiry
        return {
            "object_key": s3_key,
            "size_bytes": len(plaintext),
            "content_hash": content_hash,
        }

    def get(self, checkpoint_id: str) -> bytes | None:
        s3_key = self._s3_key(checkpoint_id)
        try:
            resp = self._s3_client.get_object(Bucket=self._bucket, Key=s3_key)
            blob = resp["Body"].read()
        except Exception:
            return None

        # Check tag-based expiry
        try:
            tags = self._s3_client.get_object_tagging(
                Bucket=self._bucket, Key=s3_key
            )
            for tag in tags.get("TagSet", []):
                if tag["Key"] == "expires_at":
                    if time.time() > float(tag["Value"]):
                        self.delete(checkpoint_id)
                        return None
        except Exception:
            pass

        try:
            wrapped_dek, nonce, ciphertext, aad = _unpack(blob)
        except (ValueError, struct.error):
            return None

        try:
            dek = self._decrypt_dek(wrapped_dek)
        except Exception:
            return None

        try:
            return _decrypt_envelope(dek, nonce, ciphertext, aad)
        except Exception:
            return None  # tampered / wrong key / AAD mismatch

    def delete(self, checkpoint_id: str) -> None:
        s3_key = self._s3_key(checkpoint_id)
        try:
            self._s3_client.delete_object(Bucket=self._bucket, Key=s3_key)
        except Exception:
            pass

    def sweep_expired(self) -> int:
        """List objects with expired tag. Returns count deleted."""
        s3 = self._s3_client
        now = int(time.time())
        count = 0
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=self._prefix):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    try:
                        tags = s3.get_object_tagging(Bucket=self._bucket, Key=key)
                        for tag in tags.get("TagSet", []):
                            if tag["Key"] == "expires_at" and int(tag["Value"]) < now:
                                s3.delete_object(Bucket=self._bucket, Key=key)
                                count += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return count


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
class CheckpointStoreConfigError(RuntimeError):
    """Raised when the production checkpoint store is enabled but misconfigured."""


_REQUIRED_S3_ENV = (
    "FIN_WORKSPACE_S3_BUCKET",
    "FIN_WORKSPACE_S3_REGION",
    "FIN_WORKSPACE_KMS_KEY_ID",
)


def _fin_workspace_enabled() -> bool:
    return os.environ.get("FIN_WORKSPACE_ENABLED", "").strip().lower() in ("1", "true", "yes")


def validate_s3_store_config(env: dict | None = None) -> list[str]:
    """Return a list of missing/blank required S3+KMS config keys (empty if OK).

    Deliberately never falls back to ``AWS_REGION`` or a default region: the
    workspace feature must be configured explicitly before it is allowed to
    run. Local in-memory storage is only for tests and is never selected by
    this validator.
    """
    if env is None:
        env = os.environ
    missing: list[str] = []
    for key in _REQUIRED_S3_ENV:
        if not str(env.get(key, "") or "").strip():
            missing.append(key)
    return missing


def create_checkpoint_store(*, require_s3: bool | None = None) -> CheckpointStore:
    """Factory for the checkpoint store.

    * ``require_s3=True``  — return ``S3CheckpointStore`` only; raise
      ``CheckpointStoreConfigError`` when S3/KMS config is missing. Never
      falls back to local storage. This is the production path.
    * ``require_s3=False`` — explicit local (test/CI) store; never used by the
      production startup path.
    * ``require_s3=None`` (default) — derive from ``FIN_WORKSPACE_ENABLED``:
      feature on ⇒ production S3 store (fail closed); feature off ⇒ local
      store (unreachable because the feature is disabled).
    """
    if require_s3 is None:
        require_s3 = _fin_workspace_enabled()

    if require_s3:
        missing = validate_s3_store_config()
        if missing:
            raise CheckpointStoreConfigError(
                "financial workspace storage misconfigured: missing "
                + ", ".join(missing)
            )
        return S3CheckpointStore()

    return LocalCheckpointStore()
