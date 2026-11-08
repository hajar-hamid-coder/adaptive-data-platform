"""Minimal object stores for Bronze: put-if-absent (never overwrite), exists, get."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree

from adp.config import get_settings
from adp.errors import IngestionError


class ObjectStore(Protocol):
    """Write-once object storage."""

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Store data; return False (and leave the object untouched) if key exists."""
        ...

    def exists(self, key: str) -> bool: ...

    def get(self, key: str) -> bytes: ...

    def list(self, prefix: str = "") -> list[str]:
        """Sorted keys starting with prefix."""
        ...

    def move(self, src: str, dst: str) -> None:
        """Archive move: copy then delete src. Never overwrites: if dst already exists with
        identical content, src is simply removed; different content raises IngestionError."""
        ...


class LocalStore:
    """Directory-backed store; atomic via temp file + hard link (fails if target exists)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put_if_absent(self, key: str, data: bytes) -> bool:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        try:
            os.link(tmp, dest)
            return True
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def list(self, prefix: str = "") -> list[str]:
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix) and k.rsplit("/", 1)[-1][0] != ".")

    def move(self, src: str, dst: str) -> None:
        s, d = self.root / src, self.root / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(s, d)  # atomic, fails if dst exists
        except FileExistsError:
            if s.read_bytes() != d.read_bytes():
                msg = f"move {src} -> {dst}: destination exists with other content"
                raise IngestionError(msg) from None
        s.unlink()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3Store:
    """S3 / MinIO store over stdlib HTTP with SigV4; PUT uses If-None-Match: *."""

    def __init__(
        self, bucket: str, *, endpoint: str | None = None, user: str | None = None,
        password: str | None = None, region: str = "us-east-1",
    ) -> None:  # fmt: skip
        s = get_settings()
        self.bucket = bucket
        self.endpoint = (endpoint or s.adp_s3_endpoint).rstrip("/")
        self.user = user or s.minio_root_user
        self.password = password or s.minio_root_password.get_secret_value()
        self.region = region

    def _request(
        self, method: str, key: str, data: bytes = b"", extra: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:  # fmt: skip
        host = self.endpoint.split("://", 1)[1]
        path = "/" + urllib.parse.quote(f"{self.bucket}/{key}")
        cq = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted((query or {}).items())
        )
        now = dt.datetime.now(dt.UTC)
        amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        payload = hashlib.sha256(data).hexdigest()
        headers = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": amz}
        headers.update(extra or {})
        names = sorted(headers)
        canon_h = "".join(f"{n}:{headers[n]}\n" for n in names)
        signed = ";".join(names)
        canonical = f"{method}\n{path}\n{cq}\n{canon_h}\n{signed}\n{payload}"
        scope = f"{day}/{self.region}/s3/aws4_request"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        to_sign = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{digest}"
        k = _sign(f"AWS4{self.password}".encode(), day)
        for part in (self.region, "s3", "aws4_request"):
            k = _sign(k, part)
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.user}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}"
        )
        headers.pop("host")
        url = self.endpoint + path + (f"?{cq}" if cq else "")
        req = urllib.request.Request(url, data=data or None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except OSError as e:
            raise IngestionError(f"S3 unreachable at {self.endpoint}: {e}") from e

    def put_if_absent(self, key: str, data: bytes) -> bool:
        status, _ = self._request("PUT", key, data, {"if-none-match": "*"})
        if status == 412:
            return False
        if status not in (200, 201):
            raise IngestionError(f"S3 PUT {key} failed with HTTP {status}")
        return True

    def exists(self, key: str) -> bool:
        status, _ = self._request("HEAD", key)
        if status == 404:
            return False
        if status != 200:
            raise IngestionError(f"S3 HEAD {key} failed with HTTP {status}")
        return True

    def get(self, key: str) -> bytes:
        status, body = self._request("GET", key)
        if status != 200:
            raise IngestionError(f"S3 GET {key} failed with HTTP {status}")
        return body

    def list(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        token = ""
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            status, body = self._request("GET", "", query=q)
            if status != 200:
                raise IngestionError(f"S3 LIST {prefix} failed with HTTP {status}")
            root = ElementTree.fromstring(body)
            ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
            keys += [e.text or "" for e in root.iter(f"{ns}Key")]
            if (root.findtext(f"{ns}IsTruncated") or "").lower() != "true":
                return sorted(keys)
            token = root.findtext(f"{ns}NextContinuationToken") or ""

    def move(self, src: str, dst: str) -> None:
        data = self.get(src)
        if not self.put_if_absent(dst, data) and self.get(dst) != data:
            raise IngestionError(f"move {src} -> {dst}: destination exists with other content")
        self.delete(src)  # only after dst is confirmed to hold the same bytes

    def delete(self, key: str) -> None:
        """Remove an object (used after a verified archive copy, and for test cleanup)."""
        status, _ = self._request("DELETE", key)
        if status not in (200, 204, 404):
            raise IngestionError(f"S3 DELETE {key} failed with HTTP {status}")
