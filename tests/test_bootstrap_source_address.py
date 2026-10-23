"""Native socket/urllib3 source-address contracts; regression for #142908."""

import errno
import socket

import pytest

import hermes_bootstrap  # installs the production connection racers
from urllib3.util.connection import create_connection as urllib3_connect


@pytest.fixture(params=[socket.AF_INET, socket.AF_INET6], ids=["ipv4", "ipv6"])
def listener(request):
    family = request.param
    host = "127.0.0.1" if family == socket.AF_INET else "::1"
    server = socket.socket(family, socket.SOCK_STREAM)
    try:
        try:
            server.bind((host, 0))
        except OSError as exc:
            if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                pytest.skip("native IPv6 loopback unavailable")
            raise
        server.listen(1)
        server.settimeout(5)
        yield server
    finally:
        server.close()


def _source_port(family, fixed):
    if not fixed:
        return 0
    with socket.socket(family, socket.SOCK_STREAM) as reservation:
        reservation.bind(("", 0))
        return reservation.getsockname()[1]


def _exchange(server, client):
    with server.accept()[0] as peer:
        peer.settimeout(5)
        client.sendall(b"x")
        assert peer.recv(1) == b"x"
        peer.sendall(b"y")
        assert client.recv(1) == b"y"


@pytest.mark.parametrize("connect", [socket.create_connection, urllib3_connect], ids=["socket", "urllib3"])
@pytest.mark.parametrize("fixed_port", [False, True])
def test_empty_source_uses_native_family_wildcard_without_dns(listener, connect, fixed_port, monkeypatch):
    original = socket.getaddrinfo
    hosts = []

    def recording_getaddrinfo(host, *args, **kwargs):
        hosts.append(host)
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", recording_getaddrinfo)
    port = _source_port(listener.family, fixed_port)
    with connect(listener.getsockname()[:2], timeout=5, source_address=("", port)) as client:
        assert client.family == listener.family
        assert client.gettimeout() == 5
        assert not port or client.getsockname()[1] == port
        _exchange(listener, client)
    assert "" not in hosts  # the transparent observer never changes native resolver behavior


@pytest.mark.parametrize("connect", [socket.create_connection, urllib3_connect], ids=["socket", "urllib3"])
@pytest.mark.parametrize("source_host", ["localhost", "literal"])
def test_explicit_source_host_and_port_remain_usable(listener, connect, source_host):
    host = listener.getsockname()[0] if source_host == "literal" else source_host
    port = _source_port(listener.family, True)
    with connect(listener.getsockname()[:2], timeout=5, source_address=(host, port)) as client:
        assert client.getsockname()[1] == port
        assert client.getpeername()[:2] == listener.getsockname()[:2]
        _exchange(listener, client)
