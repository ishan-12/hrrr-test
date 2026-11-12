"""The network and the disk: HTTP GETs against the public bucket, bucket listings, and cache files.

Only the raw I/O calls are excluded from mutation testing; the decisions about status codes, ranges,
retries and GRIB2 completeness are plain functions with their own tests.
"""

import http.client
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from hrrr_ingest.inventory import BUCKET

ATTEMPTS = 3
S3_XML = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class FetchError(Exception):
    """A request failed: an HTTP error, a wrong range, a timeout, or a malformed listing."""


class NotPublished(FetchError):
    """HTTP 404: the object does not exist (yet)."""


def _http_get(url: str, headers: dict) -> tuple:  # pragma: no mutate
    request = urllib.request.Request(url, headers=headers)  # pragma: no mutate
    try:  # pragma: no mutate
        with urllib.request.urlopen(request, timeout=60) as response:  # pragma: no mutate
            return response.status, response.headers, response.read()  # pragma: no mutate
    except urllib.error.HTTPError as e:  # pragma: no mutate
        return e.code, e.headers, b""  # pragma: no mutate


def check_range(status: int, headers, body: bytes, start: int, end: int | None) -> None:
    """Raise FetchError unless the response is exactly bytes start..end (end None: to end of file)."""
    if status != 206:
        raise FetchError(f"expected HTTP 206 for a range request, got {status}")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", str(headers.get("Content-Range")))
    if match is None:
        raise FetchError(f"bad Content-Range {headers.get('Content-Range')!r}")
    first, last, total = (int(g) for g in match.groups())
    wanted_last = total - 1 if end is None else end
    if (first, last) != (start, wanted_last) or len(body) != last - first + 1:
        raise FetchError(f"asked for bytes {start}-{end}, got {first}-{last}/{total} with {len(body)} bytes")


def fetch(url: str, start: int | None = None, end: int | None = None, sleep=time.sleep) -> bytes:
    """GET a whole object, or bytes start..end of it. Retries timeouts, 429 and 5xx up to ATTEMPTS times."""
    headers = {} if start is None else {"Range": f"bytes={start}-{'' if end is None else end}"}
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(5 * attempt)
        try:
            status, response_headers, body = _http_get(url, headers)
        except (OSError, http.client.HTTPException) as e:
            problem = f"{type(e).__name__}: {e}"
            continue
        if status == 404:
            raise NotPublished(f"not published: {url}")
        if status == 429 or status >= 500:
            problem = f"HTTP {status}"
            continue
        if start is not None:
            check_range(status, response_headers, body, start, end)
        elif status != 200:
            raise FetchError(f"HTTP {status} for {url}")
        return body
    raise FetchError(f"{problem} for {url}, after {ATTEMPTS} attempts")


def parse_listing(xml: bytes) -> tuple[list[str], str | None]:
    """Keys on one ListObjectsV2 page, and the continuation token if there are more pages."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise FetchError(f"malformed bucket listing: {e}")
    if root.tag != S3_XML + "ListBucketResult":
        raise FetchError(f"unexpected bucket listing root {root.tag!r}")
    keys = [key.text for key in root.iter(S3_XML + "Key")]
    more = root.findtext(S3_XML + "IsTruncated") == "true"
    return keys, root.findtext(S3_XML + "NextContinuationToken") if more else None


def list_keys(prefix: str, get) -> set[str]:
    """Every key in the bucket that starts with prefix, following continuation tokens."""
    keys, token = set(), None
    while True:
        query = {"list-type": "2", "prefix": prefix}
        if token:
            query["continuation-token"] = token
        page, token = parse_listing(get(f"https://{BUCKET}/?{urllib.parse.urlencode(query)}"))
        keys.update(page)
        if token is None:
            return keys


def is_complete_grib2(data: bytes) -> bool:
    """True if data is exactly one whole GRIB2 message: 'GRIB', edition 2, its own length, '7777'."""
    return (
        data[:4] == b"GRIB"
        and data[7:8] == b"\x02"
        and int.from_bytes(data[8:16], "big") == len(data)
        and data[-4:] == b"7777"
    )


def read_cached(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:  # missing or unreadable: treated as not cached
        return None


def write_atomic(path: Path, data: bytes) -> None:
    """Write to a temporary file in the same folder, close it, then rename it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, path)
