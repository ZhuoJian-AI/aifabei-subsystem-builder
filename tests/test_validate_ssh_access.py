from unittest import mock

import validate_ssh_access


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.sent = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.sent.append(value)

    def recv(self, _size):
        return next(self.chunks, b"")

    def close(self):
        self.closed = True


def test_probe_sends_public_client_identification_before_reading_banner():
    sock = FakeSocket([b"SSH-2.0-OpenSSH_test\r\n"])
    with mock.patch.object(validate_ssh_access.socket, "create_connection", return_value=sock):
        result = validate_ssh_access.probe_ssh_banner(
            "203.0.113.10", 443, connect_timeout=2, banner_timeout=3
        )

    assert result.ssh_banner is True
    assert result.banner == "SSH-2.0-OpenSSH_test"
    assert sock.sent == [validate_ssh_access.CLIENT_IDENTIFICATION]
    assert sock.timeout == 3
    assert sock.closed is True
