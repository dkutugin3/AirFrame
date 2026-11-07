"""LAN pairing discovery survives either unavailable discovery method."""
import socket
import unittest
from unittest.mock import patch

from airframe.server import lan_addresses


class RouteProbe:
    def __init__(self, address):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def connect(self, target):
        pass

    def getsockname(self):
        return self.address, 39000


class LanAddressTests(unittest.TestCase):
    def test_unresolvable_hostname_still_advertises_route_address(self):
        # Machines with a broken DNS/search suffix can still have a valid LAN route.
        with patch("airframe.server.socket.getaddrinfo", side_effect=socket.gaierror("hostname not found")), \
             patch("airframe.server.socket.socket", return_value=RouteProbe("192.168.1.42")):
            self.assertEqual(lan_addresses(), ["192.168.1.42"])

    def test_unavailable_route_preserves_hostname_addresses(self):
        resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.42", 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        with patch("airframe.server.socket.getaddrinfo", return_value=resolved), \
             patch("airframe.server.socket.socket", side_effect=OSError("no route")):
            self.assertEqual(lan_addresses(), ["192.168.1.42"])


if __name__ == "__main__":
    unittest.main()
