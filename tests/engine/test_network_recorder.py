"""Тесты engine.network_recorder: корреляция CDP-событий в записи.

Chrome не запускается: события подаются словарями ровно в том виде, в каком
их отдаёт CdpClient (method + params + sessionId). Коррелятор — чистая
логика без сокетов, поэтому здесь проверяется только контракт «событие →
готовая запись или None + счётчики»:

- пара requestWillBeSent/responseReceived даёт одну запись с полями
  network_requests (method, url, resource_type, status, ts);
- запрос без ответа, ответ без запроса, чужой requestId и битое событие
  не роняют обработчик и не ломают порядок последующих записей;
- окно ожидания ограничено по памяти, сессии не смешивают requestId.
"""

from __future__ import annotations

import pytest

from engine.network_recorder import NetworkRecord, NetworkRecorder

FROZEN_TS = 1700000000.0


def _clock(value: float = FROZEN_TS):
    return lambda: value


def _will_be_sent(
    request_id: str,
    *,
    url: str = "https://site.test/page",
    method: str = "GET",
    resource_type: object = "Document",
    session: str | None = None,
) -> dict:
    request: dict = {"method": method, "url": url}
    params: dict = {"requestId": request_id, "request": request}
    if resource_type is not None:
        params["type"] = resource_type
    message: dict = {"method": "Network.requestWillBeSent", "params": params}
    if session is not None:
        message["sessionId"] = session
    return message


def _response_received(
    request_id: str,
    *,
    status: object = 200,
    resource_type: object = None,
    session: str | None = None,
) -> dict:
    response: dict = {}
    if status is not None:
        response["status"] = status
    params: dict = {"requestId": request_id, "response": response}
    if resource_type is not None:
        params["type"] = resource_type
    message: dict = {"method": "Network.responseReceived", "params": params}
    if session is not None:
        message["sessionId"] = session
    return message


def _loading_failed(request_id: str, session: str | None = None) -> dict:
    message: dict = {"method": "Network.loadingFailed", "params": {"requestId": request_id}}
    if session is not None:
        message["sessionId"] = session
    return message


class TestCorrelation:
    def test_pair_becomes_one_record_with_all_fields(self) -> None:
        recorder = NetworkRecorder(clock=_clock())

        assert recorder.handle(_will_be_sent("R1")) is None

        record = recorder.handle(_response_received("R1", status=204, resource_type="XHR"))
        assert record == NetworkRecord(
            ts=FROZEN_TS,
            method="GET",
            url="https://site.test/page",
            resource_type="Document",
            status=204,
        )
        assert recorder.emitted == 1
        assert recorder.pending == 0

    def test_resource_type_falls_back_to_response_type(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("R1", resource_type=None))

        record = recorder.handle(_response_received("R1", resource_type="Script", status=304))

        assert record is not None
        assert record.resource_type == "Script"

    def test_method_and_url_come_from_request_not_response(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("R1", method="POST", url="https://site.test/submit"))

        record = recorder.handle(_response_received("R1", status=201))

        assert record is not None
        assert (record.method, record.url, record.status) == (
            "POST",
            "https://site.test/submit",
            201,
        )

    def test_response_without_status_keeps_record_with_null_status(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("R1"))

        record = recorder.handle(_response_received("R1", status=None))

        assert record is not None
        assert record.status is None

    def test_records_come_back_in_event_order(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("A", url="https://a.test/1"))
        recorder.handle(_will_be_sent("B", url="https://b.test/2"))
        assert recorder.pending == 2

        first = recorder.handle(_response_received("A"))
        second = recorder.handle(_response_received("B"))

        assert [first.url, second.url] == ["https://a.test/1", "https://b.test/2"]  # type: ignore[union-attr]
        assert recorder.pending == 0

    def test_ts_is_taken_at_request_time(self) -> None:
        ticks = iter([100.0, 200.0, 300.0])
        recorder = NetworkRecorder(clock=lambda: next(ticks))

        recorder.handle(_will_be_sent("R1"))
        record = recorder.handle(_response_received("R1"))

        assert record is not None
        assert record.ts == 100.0

    def test_redirect_emits_previous_hop_and_keeps_new_request(self) -> None:
        ticks = iter([100.0, 200.0, 300.0])
        recorder = NetworkRecorder(clock=lambda: next(ticks))
        recorder.handle(_will_be_sent("R1", url="https://a.test/old"))

        redirect = _will_be_sent("R1", url="https://a.test/new")
        redirect["params"]["redirectResponse"] = {"status": 301}
        hop = recorder.handle(redirect)

        assert hop == NetworkRecord(
            ts=100.0,
            method="GET",
            url="https://a.test/old",
            resource_type="Document",
            status=301,
        )

        final = recorder.handle(_response_received("R1", status=200))
        assert final is not None
        assert (final.url, final.status) == ("https://a.test/new", 200)
        assert recorder.emitted == 2
        assert recorder.pending == 0


class TestBrokenEvents:
    def test_request_without_response_gives_nothing_and_keeps_working(self) -> None:
        recorder = NetworkRecorder(clock=_clock())

        assert recorder.handle(_will_be_sent("lost")) is None
        assert recorder.pending == 1
        assert recorder.emitted == 0

        record = recorder.handle(
            _response_received("kept", status=200)
        )  # ответ на незарегистрированный id
        assert record is None
        assert recorder.unmatched_responses == 1

        # Порядок не сломан: следующая корректная пара всё ещё даёт запись.
        recorder.handle(_will_be_sent("after"))
        assert recorder.handle(_response_received("after")) is not None
        assert recorder.emitted == 1

    def test_loading_failed_drops_pending_without_record(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("R1"))

        assert recorder.handle(_loading_failed("R1")) is None

        assert recorder.pending == 0
        assert recorder.failed_requests == 1
        assert recorder.handle(_response_received("R1")) is None
        assert recorder.unmatched_responses == 1

    def test_unknown_method_is_ignored_without_counters(self) -> None:
        recorder = NetworkRecorder(clock=_clock())

        assert recorder.handle({"method": "Page.loadEventFired", "params": {}}) is None

        assert recorder.malformed == 0
        assert recorder.unmatched_responses == 0
        assert recorder.ignored == 0

    @pytest.mark.parametrize(
        "garbage",
        [
            None,
            "not a message",
            42,
            [],
            {},
            {"method": "Network.requestWillBeSent"},
            {"method": "Network.requestWillBeSent", "params": "nope"},
            {"method": "Network.requestWillBeSent", "params": {"requestId": 7}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "R", "request": {}}},
            {"method": "Network.requestWillBeSent", "params": {"requestId": "R", "request": 5}},
            {"method": "Network.responseReceived", "params": {"requestId": "R"}},
            {"method": "Network.responseReceived", "params": {"requestId": "R", "response": 5}},
            {"method": "Network.loadingFailed", "params": {}},
        ],
    )
    def test_malformed_events_never_raise_and_are_counted(self, garbage: object) -> None:
        recorder = NetworkRecorder(clock=_clock())

        assert recorder.handle(garbage) is None  # type: ignore[arg-type]

        assert recorder.malformed >= 1
        assert recorder.ignored == recorder.malformed
        assert recorder.pending == 0
        assert recorder.emitted == 0

    def test_ignored_counts_malformed_and_unmatched(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle({"method": "Network.requestWillBeSent", "params": {}})
        recorder.handle(_response_received("stranger"))

        assert recorder.malformed == 1
        assert recorder.unmatched_responses == 1
        assert recorder.ignored == 2


class TestBounds:
    def test_pending_window_is_bounded_by_max_pending(self) -> None:
        recorder = NetworkRecorder(max_pending=3, clock=_clock())
        for index in range(5):
            recorder.handle(_will_be_sent(f"R{index}", url=f"https://site.test/{index}"))

        assert recorder.pending == 3
        assert recorder.evicted == 2

        # Вытесненный запрос пару уже не находит, остальные коррелируются.
        assert recorder.handle(_response_received("R0")) is None
        assert recorder.unmatched_responses == 1
        assert recorder.handle(_response_received("R4")).url == "https://site.test/4"  # type: ignore[union-attr]

    def test_default_window_holds_typical_traffic_without_evictions(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        for index in range(50):
            recorder.handle(_will_be_sent(f"R{index}"))

        assert recorder.pending == 50
        assert recorder.evicted == 0

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_non_positive_max_pending(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_pending"):
            NetworkRecorder(max_pending=bad)

    def test_same_request_id_in_different_sessions_do_not_cross(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("1", url="https://first.test/", session="S1"))
        recorder.handle(_will_be_sent("1", url="https://second.test/", session="S2"))

        first = recorder.handle(_response_received("1", status=201, session="S1"))
        second = recorder.handle(_response_received("1", status=202, session="S2"))

        assert (first.url, first.status) == ("https://first.test/", 201)  # type: ignore[union-attr]
        assert (second.url, second.status) == ("https://second.test/", 202)  # type: ignore[union-attr]

    def test_drop_session_clears_only_that_session(self) -> None:
        recorder = NetworkRecorder(clock=_clock())
        recorder.handle(_will_be_sent("R1", session="S1"))
        recorder.handle(_will_be_sent("R1", session="S2"))

        assert recorder.drop_session("S1") == 1

        assert recorder.pending == 1
        assert recorder.evicted == 1
        assert recorder.handle(_response_received("R1", session="S1")) is None
        assert recorder.handle(_response_received("R1", session="S2")) is not None

    def test_drop_unknown_session_is_noop(self) -> None:
        recorder = NetworkRecorder(clock=_clock())

        assert recorder.drop_session("nothing-here") == 0
        assert recorder.pending == 0
        assert recorder.evicted == 0
