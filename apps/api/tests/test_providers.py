import asyncio

import httpx
import pytest

from app import providers


def response(status: int, payload=None, headers=None):
    if payload and payload.get("choices") and "finish_reason" not in payload["choices"][0]:
        payload["choices"][0]["finish_reason"] = "stop"
    return httpx.Response(
        status,
        json=payload if payload is not None else {"error": "private provider detail"},
        headers=headers,
        request=httpx.Request("POST", "https://provider.invalid/chat/completions"),
    )


def install_client(monkeypatch, outcomes, captured=None):
    captured = captured if captured is not None else {}
    queue = list(outcomes)

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.setdefault("requests", []).append({"url": url, **kwargs})
            outcome = queue.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(providers.httpx, "AsyncClient", FakeClient)
    return captured


@pytest.fixture(autouse=True)
def provider_settings(monkeypatch):
    monkeypatch.setattr(providers.settings, "model_provider", "deepseek")
    monkeypatch.setattr(providers.settings, "model_name", "deepseek-v4-flash")
    monkeypatch.setattr(providers.settings, "deepseek_api_key", "deepseek-secret")
    monkeypatch.setattr(providers.settings, "deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(providers.settings, "model_max_retries", 2)
    monkeypatch.setattr(providers.settings, "model_retry_base_seconds", 0)
    monkeypatch.setattr(providers.settings, "model_max_tokens", 2048)


def test_deepseek_provider_uses_dedicated_credentials(monkeypatch):
    captured = install_client(
        monkeypatch,
        [response(200, {"choices": [{"message": {"content": "Grounded answer [1]"}}]})],
    )

    result = asyncio.run(providers.generate_answer("Question?", ["Evidence text."]))

    assert result == "Grounded answer [1]"
    request = captured["requests"][0]
    assert request["url"] == "https://api.deepseek.com/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer deepseek-secret"}
    assert request["json"]["model"] == "deepseek-v4-flash"
    assert request["json"]["temperature"] == 0
    assert request["json"]["thinking"] == {"type": "disabled"}
    assert request["json"]["stream"] is False
    assert request["json"]["max_tokens"] == 2048
    assert "[1] Evidence text." in request["json"]["messages"][1]["content"]


def test_transient_status_honors_retry_after_and_then_succeeds(monkeypatch):
    sleeps = []

    async def fake_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(providers.asyncio, "sleep", fake_sleep)
    captured = install_client(
        monkeypatch,
        [
            response(429, headers={"Retry-After": "2"}),
            response(503),
            response(200, {"choices": [{"message": {"content": "Answer [1]"}}]}),
        ],
    )

    assert asyncio.run(providers.generate_answer("Question?", ["Evidence."])) == "Answer [1]"
    assert len(captured["requests"]) == 3
    assert sleeps == [2.0, 0]


@pytest.mark.parametrize("exc", [httpx.ConnectError("secret"), httpx.ReadTimeout("secret")])
def test_network_failure_retries_and_is_sanitized(monkeypatch, exc):
    install_client(monkeypatch, [exc, exc, exc])
    with pytest.raises(providers.ModelUnavailableError) as caught:
        asyncio.run(providers.generate_answer("private question", ["private evidence"]))
    assert str(caught.value) == "Model provider is unavailable"


def test_non_retryable_4xx_is_not_retried_or_leaked(monkeypatch):
    captured = install_client(monkeypatch, [response(401), response(200)])
    with pytest.raises(providers.ModelProviderError) as caught:
        asyncio.run(providers.generate_answer("private question", ["private evidence"]))
    assert len(captured["requests"]) == 1
    assert "401" in str(caught.value)
    assert "private provider detail" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": 123}}]},
    ],
)
def test_invalid_responses_are_rejected(monkeypatch, payload):
    install_client(monkeypatch, [response(200, payload)])
    with pytest.raises(providers.ModelResponseError):
        asyncio.run(providers.generate_answer("Question?", ["Evidence."]))


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls"])
def test_incomplete_or_tool_responses_are_never_returned(monkeypatch, finish_reason):
    install_client(
        monkeypatch,
        [
            response(
                200,
                {
                    "choices": [
                        {"finish_reason": finish_reason, "message": {"content": "Partial [1]"}}
                    ]
                },
            )
        ],
    )
    with pytest.raises(providers.ModelResponseError):
        asyncio.run(providers.generate_answer("Question?", ["Evidence."]))


def test_insufficient_system_resource_is_retryable(monkeypatch):
    install_client(
        monkeypatch,
        [
            response(
                200,
                {
                    "choices": [
                        {
                            "finish_reason": "insufficient_system_resource",
                            "message": {"content": ""},
                        }
                    ]
                },
            )
        ],
    )
    with pytest.raises(providers.ModelUnavailableError):
        asyncio.run(providers.generate_answer("Question?", ["Evidence."]))


def test_prompt_limit_rejects_before_network(monkeypatch):
    monkeypatch.setattr(providers.settings, "model_max_prompt_chars", 10)
    with pytest.raises(providers.ModelConfigurationError):
        asyncio.run(providers.generate_answer("Question?", ["private evidence"]))


def test_missing_key_fails_before_network(monkeypatch):
    monkeypatch.setattr(providers.settings, "deepseek_api_key", "")
    with pytest.raises(providers.ModelConfigurationError):
        asyncio.run(providers.generate_answer("Question?", ["Evidence."]))


def test_cancellation_is_not_converted_to_provider_error(monkeypatch):
    install_client(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(providers.generate_answer("Question?", ["Evidence."]))
