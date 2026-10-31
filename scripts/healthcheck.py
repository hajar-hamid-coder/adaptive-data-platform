"""Vérifie la stack locale : buckets MinIO (S3 SigV4, stdlib) et PostgreSQL (pg_isready)."""

import datetime as dt
import hashlib
import hmac
import os
import re
import subprocess
import sys
import urllib.request

BUCKETS = {"bronze", "silver", "gold"}


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def list_buckets() -> set[str]:
    """GET / signé SigV4 (ListBuckets) ; renvoie les noms de buckets."""
    endpoint = os.environ.get("ADP_S3_ENDPOINT", "http://127.0.0.1:9000")
    user = os.environ.get("MINIO_ROOT_USER", "minioadmin")
    pwd = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
    host = endpoint.split("://", 1)[1]
    region, service = "us-east-1", "s3"
    now = dt.datetime.now(dt.UTC)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    empty = hashlib.sha256(b"").hexdigest()
    headers = f"host:{host}\nx-amz-content-sha256:{empty}\nx-amz-date:{amz}\n"
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = f"GET\n/\n\n{headers}\n{signed}\n{empty}"
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    k = _sign(_sign(_sign(_sign(("AWS4" + pwd).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={user}/{scope}, SignedHeaders={signed}, Signature={sig}"
    req = urllib.request.Request(
        endpoint + "/",
        headers={"x-amz-date": amz, "x-amz-content-sha256": empty, "Authorization": auth},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return set(re.findall(r"<Name>([^<]+)</Name>", resp.read().decode()))


def postgres_ready() -> str:
    """pg_isready dans le conteneur ; lève CalledProcessError si Postgres ne répond pas."""
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "pg_isready"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.strip()


def main() -> int:
    ok = True
    try:
        found = list_buckets()
        missing = BUCKETS - found
        if missing:
            print(f"KO MinIO : buckets manquants {sorted(missing)}", file=sys.stderr)
            ok = False
        else:
            print(f"OK MinIO : buckets {sorted(BUCKETS)} présents")
    except Exception as exc:
        print(f"KO MinIO : {exc}", file=sys.stderr)
        ok = False
    try:
        print(f"OK Postgres : {postgres_ready()}")
    except Exception as exc:
        print(f"KO Postgres : {exc}", file=sys.stderr)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
