"""Where the web server listens (web/app.py's listen_addresses): IPv4 and
IPv6 both, so the board's .local name opens the page on a network whose
router hands out IPv6 - there it resolves to an IPv6 address first, and
an IPv4-only server refused it ("connection refused" by name, fine by
IP - carlucci on a Wind3 HUB)."""
import socket
import threading
import urllib.request

import pytest
from waitress import create_server

from localhome.web.app import _ipv6_available, listen_addresses


def test_every_address_means_ipv4_and_ipv6():
    for host in ("0.0.0.0", "", "::"):
        assert listen_addresses(host, 5000, ipv6=True) == "0.0.0.0:5000 [::]:5000"


def test_without_ipv6_in_the_kernel_it_still_starts_on_ipv4():
    assert listen_addresses("0.0.0.0", 5000, ipv6=False) == "0.0.0.0:5000"


def test_a_specific_address_is_used_as_given():
    assert listen_addresses("127.0.0.1", 5000) == "127.0.0.1:5000"
    assert listen_addresses("fe80::1", 5000) == "[fe80::1]:5000"


@pytest.mark.skipif(not _ipv6_available(), reason="no IPv6 on this machine")
def test_the_page_answers_on_both_ipv4_and_ipv6():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    server = create_server(app, listen=listen_addresses("0.0.0.0", port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for url in (f"http://127.0.0.1:{port}/", f"http://[::1]:{port}/"):
            with urllib.request.urlopen(url, timeout=5) as res:
                assert res.read() == b"ok", url
    finally:
        server.close()
