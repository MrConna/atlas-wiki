import asyncio

from app import providers


def test_deepseek_provider_uses_dedicated_credentials(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "Grounded answer [1]"}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, **kwargs)
            return FakeResponse()

    monkeypatch.setattr(providers.settings, "model_provider", "deepseek")
    monkeypatch.setattr(providers.settings, "model_name", "deepseek-v4-flash")
    monkeypatch.setattr(providers.settings, "deepseek_api_key", "deepseek-secret")
    monkeypatch.setattr(providers.settings, "deepseek_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(providers.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(providers.generate_answer("Question?", ["Evidence text."]))

    assert result == "Grounded answer [1]"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer deepseek-secret"}
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["temperature"] == 0
    assert "[1] Evidence text." in captured["json"]["messages"][1]["content"]
