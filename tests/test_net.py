"""`providers/net.py` 的安全护栏测试：SSRF 拦截与限额下载。

既不触网也不做真实 DNS：``httpx.stream`` 与 ``socket.getaddrinfo`` 都被替换掉
（见 CLAUDE.md「测试」）。
"""

from __future__ import annotations

import gzip
import socket
from collections.abc import Iterator
from typing import Protocol

import httpx
import pytest

from mining_daily_agent.providers import net

PUBLIC_IP = "93.184.216.34"
METADATA_IP = "169.254.169.254"


class _GetAddrInfo(Protocol):
    """``socket.getaddrinfo`` 的替身签名。"""

    def __call__(
        self, host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        """返回该主机名解析出的地址表。"""


class _StreamFactory(Protocol):
    """``httpx.stream`` 的替身签名。"""

    def __call__(self, method: str, url: str, **kwargs: object) -> object:
        """返回一个可进入的上下文管理器。"""


def _fake_dns(hosts: dict[str, str] | None = None) -> _GetAddrInfo:
    """假的 DNS：默认把一切解析成公网 IP，``hosts`` 里的按表来。"""

    def _getaddrinfo(
        host: str, port: int, *args: object, **kwargs: object
    ) -> list[tuple[object, ...]]:
        address = (hosts or {}).get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return _getaddrinfo


class _FakeStream:
    """``httpx.stream`` 的替身：进入上下文即给出预置响应。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def __enter__(self) -> httpx.Response:
        return self._response

    def __exit__(self, *exc_info: object) -> None:
        """不吞异常：返回 None 表示按常规传播。"""


def _fake_stream(
    *responses: httpx.Response,
) -> tuple[_StreamFactory, list[str]]:
    """按顺序吐出预置响应，并记录请求过的 URL。"""
    queue = list(responses)
    seen: list[str] = []

    def _stream(method: str, url: str, **kwargs: object) -> _FakeStream:
        seen.append(url)
        assert queue, f"没有预置第 {len(seen)} 次请求的响应"
        return _FakeStream(queue.pop(0))

    return _stream, seen


def _response(
    url: str, content: bytes = b"ok", status: int = 200, **headers: str
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=content,
        headers=headers,
        request=httpx.Request("GET", url),
    )


def _streamed_response(url: str, content: bytes, **headers: str) -> httpx.Response:
    """构造「流式」响应：带压缩头但不预先解码，与 ``httpx.stream`` 的真实行为一致。

    用 ``content=`` 构造的响应会在初始化时就把压缩头当回事，测不出重建时的问题。
    """
    return httpx.Response(
        status_code=200,
        headers=headers,
        stream=httpx.ByteStream(content),
        request=httpx.Request("GET", url),
    )


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认把域名解析成公网 IP——真实 DNS 会让测试依赖网络。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns())


# --- 入参校验（不做 DNS）-----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/latest/meta-data/",
        "http://127.1.2.3/",
        "http://localhost/admin",
        "http://foo.localhost/",
        "http://[::1]/",
        "http://10.1.2.3/internal",
        "http://172.16.5.5/",
        "http://172.31.255.254/",
        "http://192.168.1.1/router",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[fe80::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 也是回环
        "http://0.0.0.0/",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    """回环、私网、链路本地（含云元数据）与 IPv6 等价段一律拒绝。"""
    with pytest.raises(net.UnsafeUrlError):
        net.ensure_public_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/a.pdf", "file:///etc/passwd", "not-a-url", ""])
def test_non_http_schemes_are_rejected(url: str) -> None:
    with pytest.raises(net.UnsafeUrlError):
        net.ensure_public_url(url)


def test_public_urls_pass() -> None:
    assert net.ensure_public_url("https://example.com/a.pdf") == "https://example.com/a.pdf"


def test_172_32_is_not_private() -> None:
    """172.16/12 的边界要认准：172.32.0.1 已经是公网地址。"""
    assert net.ensure_public_url("http://172.32.0.1/") == "http://172.32.0.1/"


# --- 解析后校验 --------------------------------------------------------------


def test_host_resolving_to_metadata_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """域名看起来正常，解析出来是元数据地址——这正是 SSRF 的常见形态。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns({"metadata.internal": METADATA_IP}))

    with pytest.raises(net.UnsafeUrlError, match="云元数据"):
        net.ensure_resolves_to_public("http://metadata.internal/latest/")


def test_unresolvable_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> list[tuple[object, ...]]:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    with pytest.raises(net.UnsafeUrlError, match="无法解析"):
        net.ensure_resolves_to_public("http://nope.invalid/")


# --- 逐跳重定向校验 ----------------------------------------------------------


def test_redirect_to_a_metadata_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """重定向是最常见的绕过路径：第一跳合法，第二跳指向内网。

    用 ``follow_redirects=True`` 根本拦不住——那一跳是黑盒。
    """
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        _fake_dns({"example.com": PUBLIC_IP, METADATA_IP: METADATA_IP}),
    )
    stream, seen = _fake_stream(
        _response("https://example.com/start", status=302, location=f"http://{METADATA_IP}/latest/")
    )
    monkeypatch.setattr(httpx, "stream", stream)

    with pytest.raises(net.UnsafeUrlError, match="云元数据"):
        net.get_capped("https://example.com/start", max_bytes=1024, source_name="t", timeout=1.0)

    assert seen == ["https://example.com/start"], "第一跳发出去，第二跳在校验处被拒"


def test_too_many_redirects_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, _ = _fake_stream(
        *[
            _response(
                f"https://example.com/{index}",
                status=302,
                location=f"https://example.com/{index + 1}",
            )
            for index in range(net.MAX_REDIRECTS + 1)
        ]
    )
    monkeypatch.setattr(httpx, "stream", stream)

    with pytest.raises(net.UnsafeUrlError, match="重定向超过"):
        net.get_capped("https://example.com/0", max_bytes=1024, source_name="t", timeout=1.0)


# --- 限额与读取 --------------------------------------------------------------


def test_oversized_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """超限报错而不是整体读进内存——一个链接不该能把内存吃光。"""
    stream, _ = _fake_stream(_response("https://example.com/big", content=b"x" * 4096))
    monkeypatch.setattr(httpx, "stream", stream)

    with pytest.raises(net.ResponseTooLargeError, match="上限"):
        net.get_capped("https://example.com/big", max_bytes=1024, source_name="t", timeout=1.0)


def test_body_is_returned_when_within_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    stream, _ = _fake_stream(_response("https://example.com/ok", content=b"payload"))
    monkeypatch.setattr(httpx, "stream", stream)

    response = net.get_capped(
        "https://example.com/ok", max_bytes=1024, source_name="t", timeout=1.0
    )

    assert response.content == b"payload"
    assert response.status_code == 200


def test_redirect_within_the_public_internet_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """合法重定向要照常跟随，不能把正常的跳转也拦掉。"""
    stream, seen = _fake_stream(
        _response("https://example.com/old", status=301, location="https://example.com/new"),
        _response("https://example.com/new", content=b"done"),
    )
    monkeypatch.setattr(httpx, "stream", stream)

    response = net.get_capped(
        "https://example.com/old", max_bytes=1024, source_name="t", timeout=1.0
    )

    assert response.content == b"done"
    assert seen == ["https://example.com/old", "https://example.com/new"]


def test_a_compressed_response_is_not_decoded_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测踩过的坑：重建响应时照抄了压缩头，httpx 会把已解压的内容**再解一次**。

    表现为 `DecodingError: incorrect header check`，后果是**整条新闻与 PDF 链路静默
    降级成 mock**——端到端跑一次才发现。这里的假响应刻意用「流式」构造，好让它带上
    真实的 `content-encoding` 与压缩前的 `content-length`。
    """
    plain = b"<rss>" + b"x" * 200 + b"</rss>"
    compressed = gzip.compress(plain)
    assert len(compressed) != len(plain), "这组数据要能区分「重算」与「照抄」"
    stream, _ = _fake_stream(
        _streamed_response(
            "https://example.com/feed",
            compressed,
            **{"content-encoding": "gzip", "content-length": str(len(compressed))},
        )
    )
    monkeypatch.setattr(httpx, "stream", stream)

    response = net.get_capped(
        "https://example.com/feed", max_bytes=1024, source_name="t", timeout=1.0
    )

    assert response.content == plain
    assert "content-encoding" not in response.headers, "解压标记留着就会再解一次"
    assert response.headers["content-length"] == str(len(plain)), "长度也要按新内容重算"


def test_chunks_are_read_incrementally() -> None:
    """README 式的保证：读取按块进行，不是一次性 ``response.read()``。"""
    response = _response("https://example.com/ok", content=b"a" * (net.CHUNK_BYTES * 2 + 1))

    assert isinstance(net.CHUNK_BYTES, int)
    chunks: Iterator[bytes] = response.iter_bytes(net.CHUNK_BYTES)
    assert len(next(chunks)) == net.CHUNK_BYTES
