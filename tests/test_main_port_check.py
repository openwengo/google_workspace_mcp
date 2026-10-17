import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ["MCP_ENABLE_OAUTH21"] = "false"
os.environ["WORKSPACE_MCP_STATELESS_MODE"] = "false"

import main  # noqa: E402

HOST = "127.0.0.1"


def _free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _leave_time_wait_on(port):
    """Accept one connection on port and close it server-side first, leaving
    the server end in TIME_WAIT, as a process killed mid-request does."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, port))
    listener.listen()
    client = socket.create_connection((HOST, port))
    conn, _ = listener.accept()
    conn.close()
    client.recv(1)
    client.close()
    listener.close()
    time.sleep(0.05)


def test_free_port_is_bindable():
    main.check_port_bindable(HOST, _free_port())


def test_live_listener_still_blocks_startup():
    with socket.socket() as listener:
        listener.bind((HOST, 0))
        listener.listen()
        with pytest.raises(OSError):
            main.check_port_bindable(HOST, listener.getsockname()[1])


def test_time_wait_connections_do_not_block_startup():
    port = _free_port()
    _leave_time_wait_on(port)

    with socket.socket() as plain:
        try:
            plain.bind((HOST, port))
        except OSError:
            pass
        else:
            pytest.skip("platform does not block binds on TIME_WAIT sockets")

    main.check_port_bindable(HOST, port)
