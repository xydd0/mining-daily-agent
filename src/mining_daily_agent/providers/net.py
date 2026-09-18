"""外发 HTTP 的统一护栏：SSRF 拦截、逐跳重定向校验、限额流式下载。

新闻与 PDF 两条链路都从这里取数。护栏只写一份——分散在各 provider 里迟早漏一处，
而这类漏洞的代价是**内网探测**：一个被检索结果或年报链接牵着走的地址，就可能读到
云元数据（``169.254.169.254``）或内网服务。

⚠️ 已知局限：校验与连接之间存在 **DNS rebinding** 的竞态窗口（校验时解析到公网 IP，
连接时解析到内网）。要根治得把连接钉在已校验的那个 IP 上，本轮未做——需要改传输层，
不是加一个判断能解决的。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin, urlparse

import httpx

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: 单次下载的字节上限。内置年报就有 15.6 MB，PDF 给到 50 MB；HTML 正文不该有这么大。
PDF_MAX_BYTES: Final = 50 * 1024 * 1024
HTML_MAX_BYTES: Final = 5 * 1024 * 1024
#: 跟随重定向的最大跳数。
MAX_REDIRECTS: Final = 5
#: 流式读取的块大小。
CHUNK_BYTES: Final = 64 * 1024
#: 无需 DNS 即可断定的本机主机名。
LOCAL_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeUrlError(ValueError):
    """URL 指向本机 / 私网 / 链路本地 / 保留地址，拒绝访问。"""


class ResponseTooLargeError(ValueError):
    """响应体超过大小上限。"""


def _unwrap(address: IPAddress) -> IPAddress:
    """把 IPv4-mapped IPv6（``::ffff:127.0.0.1``）还原成 IPv4。

    不还原的话 ``is_loopback`` / ``is_private`` 这类判断会漏——``::ffff:127.0.0.1``
    在 IPv6 眼里既不是回环也不是私网，但它连的就是 127.0.0.1。
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _blocked_reason(address: IPAddress) -> str | None:
    """地址不可公开访问时返回原因，否则返回 ``None``。"""
    address = _unwrap(address)
    if address.is_loopback:
        return "回环地址"
    if address.is_link_local:
        return "链路本地地址（含云元数据 169.254.169.254）"
    if address.is_private:
        return "私网地址"
    if address.is_reserved:
        return "保留地址"
    if address.is_multicast:
        return "组播地址"
    if address.is_unspecified:
        return "未指定地址"
    if not address.is_global:
        return "非公网地址"
    return None


def ensure_public_url(url: str) -> str:
    """**不做 DNS** 的廉价校验：协议、字面 IP、明显的本机主机名。

    入参校验走这一层。放在这里的理由有两个：每调一次工具就解析一次域名代价太大；
    它会把「DNS 挂了」变成「入参非法」，那是两回事。

    解析域名的那道检查在 :func:`ensure_resolves_to_public`，由下载路径调用。

    Raises:
        UnsafeUrlError: 非 http(s)、缺主机名，或字面地址就是本机 / 私网 / 元数据。
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        msg = f"只允许绝对的 http(s) 地址：{url!r}"
        raise UnsafeUrlError(msg)

    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname in LOCAL_HOSTNAMES or hostname.endswith(".localhost"):
        msg = f"拒绝访问本机地址：{url!r}"
        raise UnsafeUrlError(msg)

    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return url  # 是域名，交给下载路径解析后校验
    reason = _blocked_reason(literal)
    if reason is not None:
        msg = f"拒绝访问 {hostname}：{reason}"
        raise UnsafeUrlError(msg)
    return url


def ensure_resolves_to_public(url: str) -> None:
    """解析域名并逐个校验目标 IP（字面 IP 也走这里，行为一致）。

    Raises:
        UnsafeUrlError: 解析失败，或任一目标 IP 不可公开访问。
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        msg = f"URL 缺少主机名：{url!r}"
        raise UnsafeUrlError(msg)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        msg = f"域名无法解析：{parsed.hostname}"
        raise UnsafeUrlError(msg) from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        reason = _blocked_reason(address)
        if reason is not None:
            msg = f"拒绝访问 {parsed.hostname}（{address}）：{reason}"
            raise UnsafeUrlError(msg)


def get_capped(
    url: str,
    *,
    max_bytes: int,
    source_name: str,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
) -> httpx.Response:
    """带护栏的 GET：逐跳校验目标地址，流式读取并设大小上限。

    刻意**不用** ``follow_redirects=True``：那样重定向是黑盒，中途跳到
    ``169.254.169.254`` 这类云元数据地址根本拦不住。这里自己跟随，每一跳都校验。

    Returns:
        一个内容已读完的响应（形状与 ``httpx.get`` 一致，调用方无需改动）。

    Raises:
        UnsafeUrlError: 任一跳的地址不可公开访问，或重定向次数超限。
        ResponseTooLargeError: 响应体超过 ``max_bytes``。
        httpx.HTTPError: 传输层错误。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        ensure_public_url(current)
        ensure_resolves_to_public(current)
        with httpx.stream(
            "GET",
            current,
            headers=headers,
            params=params,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            location = response.headers.get("location")
            if response.is_redirect and location:
                current = urljoin(current, location)
                logger.debug(
                    "跟随重定向：source=%s from=%s to=%s", source_name, response.url, current
                )
                continue
            return _read_capped(response, max_bytes=max_bytes, source_name=source_name)

    msg = f"重定向超过 {MAX_REDIRECTS} 跳：{url}"
    raise UnsafeUrlError(msg)


def _read_capped(response: httpx.Response, *, max_bytes: int, source_name: str) -> httpx.Response:
    """流式读完整响应，超过上限即抛错，不整体读进内存。"""
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            msg = f"响应超过 {max_bytes} 字节上限：source={source_name} url={response.url}"
            raise ResponseTooLargeError(msg)
        chunks.append(chunk)
    return httpx.Response(
        status_code=response.status_code,
        headers=response.headers,
        content=b"".join(chunks),
        request=response.request,
    )
