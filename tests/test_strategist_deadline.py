"""Public API contract; production-derived narrative omitted."""
from contextlib import contextmanager
import http.client
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from .test_strategist_fallback import strategist, _body, _Response, _completion


class Clock:
    value = 0.0
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def clock(strategist, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(strategist.time, "monotonic", clock.now)
    monkeypatch.setattr(strategist.time, "sleep", clock.sleep)
    return clock


def test_admission_wait_subtracts_from_http_budget(strategist, monkeypatch, clock):
    waits, posts = [], []
    @contextmanager
    def lease(timeout):
        waits.append(timeout)
        clock.sleep(4)
        yield
    def post(request, timeout):
        posts.append(timeout)
        return _Response(_completion())
    monkeypatch.setattr(strategist, "trader_glm_lease", lease)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", post)
    strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist), 10)
    assert waits == [10]
    assert posts == [6]


def test_retries_share_original_budget_and_do_not_fallback_when_exhausted(
        strategist, monkeypatch, clock):
    calls = []
    def post(request, timeout):
        calls.append((request.full_url, timeout))
        clock.sleep(4)
        raise urllib.error.URLError("synthetic transient error")
    monkeypatch.setattr(strategist.urllib.request, "urlopen", post)
    with pytest.raises(strategist.ModelDeadlineExceeded):
        strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist),
                              10, retries=5, backoff=1)
    assert calls == [(strategist._GLM_PRIMARY_ENDPOINT, 10),
                     (strategist._GLM_PRIMARY_ENDPOINT, 5)]


def test_fallback_gets_only_remaining_time(strategist, monkeypatch, clock):
    calls = []
    def post(request, timeout):
        calls.append((request.full_url, timeout))
        if len(calls) == 1:
            clock.sleep(6)
            raise urllib.error.URLError("primary unavailable")
        return _Response(_completion("fallback"))
    monkeypatch.setattr(strategist.urllib.request, "urlopen", post)
    result = strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT,
                                   _body(strategist), 10, retries=1)
    assert result["choices"][0]["message"]["content"] == "fallback"
    assert calls == [(strategist._GLM_PRIMARY_ENDPOINT, 10),
                     (strategist._DEEPSEEK_FALLBACK_ENDPOINT, 4)]


def test_exhausted_admission_never_sends_http_or_fallback(strategist, monkeypatch, clock):
    calls = []
    @contextmanager
    def lease(timeout):
        clock.sleep(timeout)
        raise strategist.LeaseError("slot stayed occupied")
        yield
    monkeypatch.setattr(strategist, "trader_glm_lease", lease)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    with pytest.raises(strategist.ModelDeadlineExceeded):
        strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist), 10)
    assert calls == []


def test_identity_cannot_spend_budget_then_start_generation(strategist, monkeypatch, clock):
    calls = []
    def snapshot(endpoint, **kwargs):
        clock.sleep(10)
        return {"verified": True}
    monkeypatch.setattr(strategist.provenance, "runtime_snapshot", snapshot)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    with pytest.raises(strategist.ModelDeadlineExceeded):
        strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist),
                              10, return_identity=True)
    assert calls == []


def test_late_valid_output_is_not_accepted(strategist, monkeypatch, clock):
    calls = []
    def post(request, timeout):
        calls.append(request.full_url)
        return _Response(_completion())
    def validate(_posted):
        clock.sleep(11)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", post)
    with pytest.raises(strategist.ModelDeadlineExceeded):
        strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist),
                              10, response_validator=validate)
    assert calls == [strategist._GLM_PRIMARY_ENDPOINT]


def test_deadline_closes_response_before_releasing_lease(strategist, monkeypatch, clock):
    events = []
    @contextmanager
    def lease(_timeout):
        try:
            yield
        finally:
            events.append("lease_released")
    class Response(_Response):
        def read(self):
            clock.sleep(11)
            return _completion()
        def __exit__(self, *args):
            events.append("response_closed")
    monkeypatch.setattr(strategist, "trader_glm_lease", lease)
    monkeypatch.setattr(strategist.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(strategist.ModelDeadlineExceeded):
        strategist._post_json(strategist._GLM_PRIMARY_ENDPOINT, _body(strategist), 10)
    assert events == ["response_closed", "lease_released"]


def test_native_keepalive_body_cannot_reset_deadline(strategist, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    disconnected = threading.Event()
    finish = threading.Event()
    client, server = socket.socketpair()
    def send_keepalives():
        try:
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
            while not finish.wait(0.02):
                try:
                    if server.recv(1, socket.MSG_DONTWAIT) == b"":
                        disconnected.set()
                        return
                except BlockingIOError:
                    pass
                server.sendall(b" ")
        except (BrokenPipeError, ConnectionResetError):
            disconnected.set()
    worker = threading.Thread(target=send_keepalives, daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        with http.client.HTTPResponse(client) as response:
            response.begin()
            with pytest.raises(strategist.ModelDeadlineExceeded):
                strategist._read_model_response(response, started + 0.2)
        elapsed = time.monotonic() - started
        assert elapsed < 0.8
        assert disconnected.wait(0.5), "server did not observe socket close"
    finally:
        finish.set()
        client.close()
        server.close()
        worker.join(timeout=1)


@pytest.mark.parametrize("framing", ["content_length", "chunked", "empty"])
def test_completed_http_body_may_close_its_socket_before_next_read(strategist, framing):
    """Public API contract; production-derived narrative omitted."""
    payload = b'{"status":"healthy"}'
    if framing == "content_length":
        wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    elif framing == "chunked":
        wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + format(len(payload), "x").encode() + b"\r\n" + payload + b"\r\n0\r\n\r\n")
    else:
        payload = b""
        wire = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
    client, server = socket.socketpair()
    try:
        server.sendall(wire)
        with http.client.HTTPResponse(client) as response:
            response.begin()

            client.close()
            assert strategist._read_model_response(response, time.monotonic() + 1) == payload
            assert response.fp is None
            assert client.fileno() == -1
    finally:
        client.close()
        server.close()
