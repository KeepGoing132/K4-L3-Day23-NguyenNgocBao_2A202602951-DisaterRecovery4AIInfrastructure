"""Additional isolated checks for safety paths not exercised by the supplied tests."""
import json
import types

import httpx
import pytest

from dr import failover as fo
from dr import health_checker as hc
from dr import runbook as rb


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fake_clock():
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    return types.SimpleNamespace(monotonic=lambda: clock["now"], time=lambda: 1700000000 + clock["now"],
                                 sleep=sleep, strftime=rb.time.strftime, gmtime=rb.time.gmtime)


def test_threshold_is_per_region_and_resets_after_success(monkeypatch, tmp_path):
    sequences = {"a": iter([True, False, True, False, False, False, True]),
                 "b": iter([True] * 7)}
    monkeypatch.setattr(hc, "time", fake_clock())
    monkeypatch.setattr(hc, "probe", lambda region, timeout: (next(sequences[region]), "test"))
    log = tmp_path / "health.jsonl"
    hc.run(1, 0.1, 3, 7, log)
    events = read_events(log)
    assert [(event["region"], event["to"]) for event in events] == [
        ("a", "UNHEALTHY"), ("a", "HEALTHY")]
    assert events[0]["consecutive_fails"] == 3
    assert events[1]["consecutive_fails"] == 0


@pytest.mark.parametrize("ready", [False, True])
def test_cutover_requires_readiness_after_restore_and_scale(monkeypatch, tmp_path, ready):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "edge").mkdir()
    (tmp_path / "edge/active_region").write_text("a")
    (tmp_path / "state/region-b").mkdir(parents=True)
    monkeypatch.setattr(fo, "LOG", tmp_path / "failover.jsonl")
    monkeypatch.setattr(fo, "state_of", lambda region: {
        "region": region, "pool_state": "warm", "count": 200, "weights": True})
    monkeypatch.setattr(fo.snapshot, "get", lambda *args: {
        "source_region": "a", "snapshot_at": 1700000000,
        "embed_model_version": "vi-e5-base@v3"})
    monkeypatch.setattr(fo.snapshot, "rpo", lambda *args: {"rpo_seconds": 20, "docs_lost": 2})
    probes = []

    def readiness(url, timeout):
        probes.append(url)
        assert (tmp_path / "state/region-b/pool_state").read_text() == "full"
        assert (tmp_path / "edge/active_region").read_text() == "a"
        return httpx.Response(200 if ready else 503, json={"ready": ready, "vectors": {"count": 200}})

    monkeypatch.setattr(fo.httpx, "get", readiness)
    result = fo.failover("b", "fs", 0.03)
    events = read_events(fo.LOG)
    assert probes, "must reach the readiness poll rather than aborting during preflight"
    assert result["ok"] is ready
    assert (tmp_path / "edge/active_region").read_text() == ("b" if ready else "a")
    steps = [event["step"] for event in events if "step" in event]
    expected = ["1_verify_target", "2_restore_snapshot", "3_scale_pool"]
    assert steps == expected + (["4_wait_ready", "5_dns_cutover"] if ready else [])
    if not ready:
        assert result["failed_step"] == "4_wait_ready"
        assert events[-1]["event"] == "failover_aborted"


@pytest.mark.parametrize("answer, accepted", [("", False), ("n", False), ("y", True), ("YES", True)])
def test_confirmation_defaults_to_no(monkeypatch, answer, accepted):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert rb.confirm(False, "Proceed?") is accepted


@pytest.mark.parametrize("approve", [False, True])
def test_runbook_single_failover_and_ten_real_client_calls(monkeypatch, tmp_path, approve):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rb, "LOG", tmp_path / "runbook.jsonl")
    monkeypatch.setattr(fo, "LOG", tmp_path / "failover.jsonl")
    monkeypatch.setattr(rb, "time", fake_clock())
    monkeypatch.setattr(rb.hc, "probe", lambda *args: (False, "ReadTimeout"))
    monkeypatch.setattr(rb.httpx, "get", lambda *args, **kwargs: httpx.Response(200))
    monkeypatch.setattr(rb, "confirm", lambda *args: approve)
    calls = []
    result = {"ok": True, "target": "b", "cutover_ts": 1700000010,
              "state": {"count": 200, "weights": True}, "rpo_seconds": 20,
              "docs_lost": 2, "embed_model_version": "test"}

    def failover(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(rb.fo, "failover", failover)
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            requests.append(url)
            return httpx.Response(200, json={"region": "b"})

    monkeypatch.setattr(rb.httpx, "Client", Client)
    output = rb.run("a", "b", "fs", auto=False)
    assert output["ok"] is approve
    assert len(calls) == (1 if approve else 0)
    assert len(requests) == (10 if approve else 0)
    events = read_events(rb.LOG)
    if approve:
        assert [event["step"] for event in events] == list(range(1, 8))
        assert events[5]["error_rate"] == 0
        assert len(read_events(fo.LOG)) == 10
