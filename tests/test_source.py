"""T4 (HTTP rules), bucket listings and the cache's completeness check. No network: _http_get is faked."""

import http.client
from pathlib import Path

import pytest

from hrrr_ingest import source

URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.20250424/conus/hrrr.t06z.wrfsfcf00.grib2"
FIXTURES = Path(__file__).parent / "fixtures"


def serve(monkeypatch, *responses):
    """Make _http_get return (or raise) the given responses in order; returns the list of requests."""
    requests = []

    def fake(url, headers):
        requests.append((url, headers))
        response = responses[len(requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(source, "_http_get", fake)
    return requests


def fetch(*args):
    sleeps = []
    return source.fetch(*args, sleep=sleeps.append), sleeps


def test_whole_object(monkeypatch):
    requests = serve(monkeypatch, (200, {}, b"hello"))
    assert fetch(URL) == (b"hello", [])
    assert requests == [(URL, {})]


def test_byte_range(monkeypatch):
    requests = serve(monkeypatch, (206, {"Content-Range": "bytes 10-14/100"}, b"12345"))
    assert fetch(URL, 10, 14) == (b"12345", [])
    assert requests == [(URL, {"Range": "bytes=10-14"})]


def test_open_ended_byte_range_runs_to_the_end_of_the_file(monkeypatch):
    requests = serve(monkeypatch, (206, {"Content-Range": "bytes 95-99/100"}, b"12345"))
    assert fetch(URL, 95, None) == (b"12345", [])
    assert requests == [(URL, {"Range": "bytes=95-"})]


@pytest.mark.parametrize(
    "status, content_range, body, message",
    [
        (200, None, b"x" * 100, "expected HTTP 206 for a range request, got 200"),
        (206, None, b"12345", "bad Content-Range None"),
        (206, "bytes 10-14", b"12345", "bad Content-Range 'bytes 10-14'"),
        (206, "bytes 11-14/100", b"1234", "asked for bytes 10-14, got 11-14/100 with 4 bytes"),
        (206, "bytes 10-15/100", b"123456", "asked for bytes 10-14, got 10-15/100 with 6 bytes"),
        (206, "bytes 10-14/100", b"1234", "asked for bytes 10-14, got 10-14/100 with 4 bytes"),
        (206, "bytes 10-14/100", b"123456", "asked for bytes 10-14, got 10-14/100 with 6 bytes"),
    ],
)
def test_a_range_response_must_match_exactly(monkeypatch, status, content_range, body, message):
    headers = {} if content_range is None else {"Content-Range": content_range}
    requests = serve(monkeypatch, (status, headers, body))
    with pytest.raises(source.FetchError) as exc:
        fetch(URL, 10, 14)
    assert str(exc.value) == message
    assert len(requests) == 1


def test_an_open_ended_range_must_reach_the_end_of_the_file(monkeypatch):
    serve(monkeypatch, (206, {"Content-Range": "bytes 95-98/100"}, b"1234"))
    with pytest.raises(source.FetchError) as exc:
        fetch(URL, 95, None)
    assert str(exc.value) == "asked for bytes 95-None, got 95-98/100 with 4 bytes"


def test_404_means_not_published_and_is_not_retried(monkeypatch):
    requests = serve(monkeypatch, (404, {}, b""))
    with pytest.raises(source.NotPublished) as exc:
        fetch(URL + ".idx")
    assert str(exc.value) == f"not published: {URL}.idx"
    assert len(requests) == 1


@pytest.mark.parametrize("status", [403, 499, 304])
def test_other_errors_fail_at_once(monkeypatch, status):
    requests = serve(monkeypatch, (status, {}, b""))
    with pytest.raises(source.FetchError) as exc:
        fetch(URL)
    assert type(exc.value) is source.FetchError
    assert str(exc.value) == f"HTTP {status} for {URL}"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure, problem",
    [
        ((500, {}, b""), "HTTP 500"),
        ((503, {}, b""), "HTTP 503"),
        ((429, {}, b""), "HTTP 429"),
        (TimeoutError("timed out"), "TimeoutError: timed out"),
        (ConnectionResetError("reset"), "ConnectionResetError: reset"),
        (http.client.IncompleteRead(b"ab", 3), "IncompleteRead: IncompleteRead(2 bytes read, 3 more expected)"),
    ],
)
def test_transient_failures_are_retried_with_backoff_then_give_up(monkeypatch, failure, problem):
    requests = serve(monkeypatch, failure, failure, failure)
    sleeps = []
    with pytest.raises(source.FetchError) as exc:
        source.fetch(URL, sleep=sleeps.append)
    assert str(exc.value) == f"{problem} for {URL}, after 3 attempts"
    assert len(requests) == 3
    assert sleeps == [5, 10]


def test_a_retry_that_succeeds_returns_the_body(monkeypatch):
    requests = serve(monkeypatch, (503, {}, b""), TimeoutError("t"), (200, {}, b"ok"))
    assert fetch(URL) == (b"ok", [5, 10])
    assert len(requests) == 3


def test_check_range_reads_the_header_case_insensitively_from_a_real_message_object():
    headers = http.client.HTTPMessage()
    headers["content-range"] = "bytes 0-1/2"
    source.check_range(206, headers, b"ab", 0, 1)


# --- listings ------------------------------------------------------------------------------------

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def listing(keys, truncated, token=None):
    xml = f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult {NS}><Name>noaa-hrrr-bdp-pds</Name>'
    xml += f"<IsTruncated>{truncated}</IsTruncated>"
    if token:
        xml += f"<NextContinuationToken>{token}</NextContinuationToken>"
    xml += "".join(f"<Contents><Key>{k}</Key><Size>1</Size></Contents>" for k in keys)
    return (xml + "</ListBucketResult>").encode()


def test_parse_listing():
    assert source.parse_listing(listing(["a", "b"], "true", "tok+/=")) == (["a", "b"], "tok+/=")
    assert source.parse_listing(listing(["c"], "false", "ignored")) == (["c"], None)
    assert source.parse_listing(listing([], "false")) == ([], None)


def test_listing_that_is_not_xml_is_a_fetch_error():
    with pytest.raises(source.FetchError) as exc:
        source.parse_listing(b"<ListBucketResult")
    assert str(exc.value).startswith("malformed bucket listing: ")


def test_listing_with_an_unexpected_root_is_a_fetch_error():
    with pytest.raises(source.FetchError) as exc:
        source.parse_listing(b"<Error><Code>AccessDenied</Code></Error>")
    assert str(exc.value) == "unexpected bucket listing root 'Error'"


def test_list_keys_follows_continuation_tokens():
    pages = [listing(["k1", "k2"], "true", "next/token="), listing(["k3"], "false")]
    urls = []

    def get(url):
        urls.append(url)
        return pages[len(urls) - 1]

    assert source.list_keys("hrrr.20250424/conus/hrrr.t06z.wrfsfcf", get) == {"k1", "k2", "k3"}
    base = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/?list-type=2&prefix=hrrr.20250424%2Fconus%2Fhrrr.t06z.wrfsfcf"
    assert urls == [base, base + "&continuation-token=next%2Ftoken%3D"]


# --- cache completeness --------------------------------------------------------------------------


def test_real_messages_are_complete():
    for name in ("f00_vbdsf_surface.grib2", "f00_tmp_2m.grib2"):
        assert source.is_complete_grib2((FIXTURES / name).read_bytes())


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda m: m[:-1],  # truncated
        lambda m: m + b"7777",  # extra bytes
        lambda m: b"X" + m[1:],  # bad start marker
        lambda m: m[:-1] + b"8",  # bad end marker
        lambda m: m[:7] + b"\x01" + m[8:],  # GRIB edition 1
        lambda m: m[:8] + bytes([1]) + m[9:],  # length field high byte set
        lambda m: m[:15] + bytes([m[15] + 1]) + m[16:],  # length field low byte off by one
        lambda m: b"",
    ],
)
def test_incomplete_or_corrupt_messages_are_rejected(corrupt):
    message = (FIXTURES / "f00_vbdsf_surface.grib2").read_bytes()
    assert not source.is_complete_grib2(corrupt(message))
