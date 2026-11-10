"""Every response and log line carries a request id; caller ids are kept only when safe to log."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.request_id import RequestIdFilter, choose_request_id, current_request_id
from app.main import app


class TestChooseRequestId:
    def test_a_safe_caller_id_is_kept(self):
        assert choose_request_id("trace-123_ab.C") == "trace-123_ab.C"

    @pytest.mark.parametrize("incoming", [None, "", "x" * 65, "evil\nINFO fake log line", "a b", "<script>"])
    def test_anything_else_gets_a_new_id(self, incoming):
        new = choose_request_id(incoming)
        assert new != incoming and len(new) == 16 and new.isalnum()


def test_responses_carry_the_request_id():
    client = TestClient(app)
    assert len(client.get("/").headers["x-request-id"]) == 16
    assert client.get("/", headers={"X-Request-ID": "from-proxy-1"}).headers["x-request-id"] == "from-proxy-1"


def test_log_records_get_the_request_id_and_a_dash_outside_requests():
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "message", None, None)
    assert RequestIdFilter().filter(record) and record.request_id == current_request_id() == "-"
