"""Runnable checks for Groq 3-key rotation (app/llm.py).

Mocked only — no real API keys, no network. Run with:
    python tests/test_groq_rotation.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config
from app import llm


def _reset_state():
    llm._key_pos = 0
    llm._quarantined.clear()
    config.GROQ_API_KEY_1 = "fake-key-0"
    config.GROQ_API_KEY_2 = "fake-key-1"
    config.GROQ_API_KEY_3 = "fake-key-2"
    config.LLM_API_KEY = ""


def _ok_response(content="hello"):
    req = httpx.Request("POST", config.LLM_BASE_URL)
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=req)


def _err_response(status):
    req = httpx.Request("POST", config.LLM_BASE_URL)
    return httpx.Response(status, request=req)


def _key_of(call_kwargs) -> str:
    return call_kwargs["headers"]["Authorization"].replace("Bearer ", "")


def test_rotation_order():
    _reset_state()
    got = [llm._next_healthy_key()[0] for _ in range(6)]
    assert got == [0, 1, 2, 0, 1, 2], f"rotation order wrong: {got}"
    print("PASS rotation order 0,1,2,0,1,2")


def test_failover_on_429():
    _reset_state()
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"headers": headers, "model": json["model"]})
        key = _key_of({"headers": headers})
        if key == "fake-key-0":
            return _err_response(429)  # no retry-after header -> no sleep
        return _ok_response("after-failover")

    orig = httpx.post
    httpx.post = fake_post
    try:
        out = llm._post({"model": "openai/gpt-oss-120b", "messages": []}, timeout=5)
    finally:
        httpx.post = orig
    assert out == "after-failover", f"expected failover content, got {out!r}"
    assert len(calls) == 2, f"expected 2 calls, got {len(calls)}"
    assert _key_of(calls[0]) == "fake-key-0"
    assert _key_of(calls[1]) == "fake-key-1"
    assert calls[0]["model"] == "openai/gpt-oss-120b"
    print("PASS 429 failover key0 -> key1")


def test_quarantine_on_401():
    _reset_state()
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"headers": headers})
        key = _key_of({"headers": headers})
        if key == "fake-key-0":
            return _err_response(401)
        return _ok_response("quarantined-ok")

    orig = httpx.post
    httpx.post = fake_post
    try:
        out = llm._post({"model": "openai/gpt-oss-120b", "messages": []}, timeout=5)
    finally:
        httpx.post = orig
    assert out == "quarantined-ok"
    assert 0 in llm._quarantined, "key 0 should be quarantined after 401"
    # key 0 must never be picked again
    for _ in range(4):
        assert llm._next_healthy_key()[0] != 0
    print("PASS 401 quarantine")


def test_llama_only_after_all_keys_tried_with_gptoss():
    _reset_state()
    models_seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        models_seen.append(json["model"])
        if "gpt-oss" in json["model"]:
            return _err_response(500)
        return _ok_response("llama-rescue")

    orig = httpx.post
    httpx.post = fake_post
    try:
        out = llm._post({"model": "openai/gpt-oss-120b", "messages": []}, timeout=5)
    finally:
        httpx.post = orig
    assert out == "llama-rescue", f"got {out!r}"
    gpt_calls = [m for m in models_seen if "gpt-oss" in m]
    first_llama_at = next(i for i, m in enumerate(models_seen) if "llama" in m)
    assert len(gpt_calls) == 3, f"expected 3 gpt-oss attempts (one per key), got {len(gpt_calls)}"
    assert first_llama_at == 3, f"llama tried too early at position {first_llama_at}: {models_seen}"
    print("PASS llama fallbacks only after all keys tried with gpt-oss-120b")


def test_no_keys_returns_none_without_network():
    _reset_state()
    config.GROQ_API_KEY_1 = config.GROQ_API_KEY_2 = config.GROQ_API_KEY_3 = ""
    config.LLM_API_KEY = ""

    def boom(*a, **k):
        raise AssertionError("network should not be touched with no keys")

    orig = httpx.post
    httpx.post = boom
    try:
        assert llm._post({"model": "openai/gpt-oss-120b", "messages": []}) is None
    finally:
        httpx.post = orig
    print("PASS no keys -> None, no network")


def test_legacy_key_as_fourth_fallback():
    _reset_state()
    config.GROQ_API_KEY_1 = config.GROQ_API_KEY_2 = config.GROQ_API_KEY_3 = ""
    config.LLM_API_KEY = "legacy-key"
    pool = llm._key_pool()
    assert pool == [(3, "legacy-key")], f"unexpected pool: {pool}"
    assert llm._next_healthy_key() == (3, "legacy-key")
    print("PASS legacy LLM_API_KEY used as 4th fallback")


def test_no_key_value_in_logs(cap=None):
    # Failover logging must reference key indexes, never values.
    _reset_state()
    records = []

    class H:
        def handle(self, r):
            records.append(r.getMessage())

    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    llm.logger.addHandler(handler)
    try:
        def fake_post(url, headers=None, json=None, timeout=None):
            return _err_response(429)

        orig = httpx.post
        httpx.post = fake_post
        try:
            llm._post({"model": "openai/gpt-oss-120b", "messages": []}, timeout=5)
        finally:
            httpx.post = orig
    finally:
        llm.logger.removeHandler(handler)
    blob = "\n".join(records)
    assert "fake-key-0" not in blob and "fake-key-1" not in blob and "fake-key-2" not in blob, \
        f"key value leaked into logs: {blob[:300]}"
    assert "key #0" in blob, "expected key index in logs"
    print("PASS logs contain key index only, never key values")


if __name__ == "__main__":
    import logging

    test_rotation_order()
    test_failover_on_429()
    test_quarantine_on_401()
    test_llama_only_after_all_keys_tried_with_gptoss()
    test_no_keys_returns_none_without_network()
    test_legacy_key_as_fourth_fallback()
    test_no_key_value_in_logs()
    print("ALL GROQ ROTATION CHECKS PASSED")
