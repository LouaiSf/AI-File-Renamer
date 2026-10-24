import json
import unittest
from unittest import mock

import httpx

from ai_renamer.config import Provider
from ai_renamer.content import Content
from ai_renamer.llm import Client, LLMError, build_messages, parse_reply, suggest


def provider(**kw) -> Provider:
    base = dict(id="t", label="Test", kind="openai", base_url="http://test/v1", model="m", api_key="k", rpm=0, workers=1)
    return Provider(**{**base, **kw})


def reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def client_with(handler, **kw) -> Client:
    c = Client(provider(**kw))
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


class ParseReplyTests(unittest.TestCase):
    def test_plain_json(self):
        s = parse_reply('{"title": "ACME Invoice", "category": "Invoice", "date": "2025-03-04", "date_relevant": true, "confidence": 0.9}')
        self.assertEqual((s.title, s.date, s.date_relevant, s.confidence), ("ACME Invoice", "2025-03-04", True, 0.9))

    def test_fenced_and_thinking(self):
        s = parse_reply('<think>hmm {not json}</think>\n```json\n{"title": "Recipe", "date": null, "date_relevant": "true"}\n```')
        self.assertEqual(s.title, "Recipe")
        self.assertFalse(s.date_relevant)  # no date -> never relevant

    def test_bad_date_dropped_and_confidence_clamped(self):
        s = parse_reply('{"title": "X", "date": "March 2025", "date_relevant": true, "confidence": 7}')
        self.assertIsNone(s.date)
        self.assertEqual(s.confidence, 1.0)

    def test_missing_title_raises(self):
        with self.assertRaises(ValueError):
            parse_reply('{"category": "x"}')


class ClientTests(unittest.TestCase):
    def test_sends_json_mode_and_auth(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=reply("{}"))
        c = Client(provider())
        c.http = httpx.Client(transport=httpx.MockTransport(handler), headers=c.http.headers)
        c.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(seen["auth"], "Bearer k")
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})

    def test_falls_back_when_json_mode_unsupported(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append("response_format" in body)
            if "response_format" in body:
                return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
            return httpx.Response(200, json=reply("ok"))
        self.assertEqual(client_with(handler).chat([]), "ok")
        self.assertEqual(calls, [True, False])

    @mock.patch("ai_renamer.llm.time.sleep")
    def test_retries_rate_limit(self, sleep):
        responses = [httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "slow down"}}),
                     httpx.Response(200, json=reply("done"))]
        self.assertEqual(client_with(lambda r: responses.pop(0)).chat([]), "done")
        sleep.assert_called_with(3.0)

    def test_error_message_surfaces(self):
        c = client_with(lambda r: httpx.Response(401, json=[{"error": {"message": "API key not valid"}}]))
        with self.assertRaisesRegex(LLMError, "API key not valid"):
            c.chat([])

    def test_missing_key_is_reported_before_request(self):
        with self.assertRaisesRegex(LLMError, "No API key"):
            client_with(lambda r: httpx.Response(200), api_key=None).chat([])

    def test_ollama_connection_error_is_friendly(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        with self.assertRaisesRegex(LLMError, "Ollama"):
            client_with(handler, kind="ollama", api_key=None).chat([])

    def test_ollama_uses_native_api_with_images_and_no_thinking(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"message": {"content": '{"title": "X"}'}})
        c = client_with(handler, kind="ollama", api_key=None, base_url="http://127.0.0.1:11434/v1")
        msgs = build_messages("P", Content(text="hello", images=[b"img"]))
        self.assertEqual(c.chat(msgs), '{"title": "X"}')
        self.assertEqual(seen["url"], "http://127.0.0.1:11434/api/chat")
        body = seen["body"]
        self.assertEqual((body["think"], body["format"], body["stream"]), (False, "json", False))
        self.assertEqual(body["messages"][1]["images"], ["aW1n"])
        self.assertIn("hello", body["messages"][1]["content"])

    def test_ollama_model_without_think_switch(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append("think" in body)
            if "think" in body:
                return httpx.Response(400, json={"error": "model does not support thinking"})
            return httpx.Response(200, json={"message": {"content": "ok"}})
        self.assertEqual(client_with(handler, kind="ollama", api_key=None).chat([]), "ok")
        self.assertEqual(calls, [True, False])

    def test_list_models_strips_prefix(self):
        c = client_with(lambda r: httpx.Response(200, json={"data": [{"id": "models/gemini-x"}, {"id": "b"}]}))
        self.assertEqual(c.list_models(), ["b", "gemini-x"])

    def test_suggest_repairs_bad_json_once(self):
        answers = ["Sure! Here's a name: Invoice", '{"title": "ACME Invoice"}']
        c = client_with(lambda r: httpx.Response(200, json=reply(answers.pop(0))))
        self.assertEqual(suggest(c, "prompt", Content(text="invoice")).title, "ACME Invoice")


class MessageTests(unittest.TestCase):
    def test_images_are_attached_as_data_urls(self):
        msgs = build_messages("P", Content(images=[b"\xff\xd8jpeg"], hints={"current_filename": "IMG_1.jpg"}))
        parts = msgs[1]["content"]
        self.assertIn("IMG_1.jpg", parts[0]["text"])
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
