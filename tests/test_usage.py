import json
import tempfile
import unittest
from pathlib import Path

from wisland.services.usage import (balance_text, format_limits, latest_session_tokens,
                                   record_usage, token_totals)


class UsageTests(unittest.TestCase):
    def test_only_official_deepseek_responses_count(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "usage.db"
            for base in ("https://api.deepseek.com/v1", "https://api.deepseek.com",
                         "https://api.deepseek.com.example", "https://example.org"):
                record_usage(base, {"prompt_tokens": 9, "completion_tokens": 3}, db)
            self.assertEqual(token_totals(db), (18, 6))

    def test_custom_endpoint_never_sends_key_to_balance_api(self):
        self.assertIn("自定义", balance_text("abc", "https://example.org/v1"))

    def test_codex_weekly_limit_and_business_credits(self):
        snapshot = {"primary": {"usedPercent": 3, "windowDurationMins": 10080},
                    "credits": {"balance": "8.25", "hasCredits": True}, "planType": "business"}
        display = format_limits({"rateLimits": snapshot})
        for expected in ("7 天", "97%", "8.25", "business"):
            self.assertIn(expected, display)

    def test_latest_session_reads_cumulative_usage_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rollout-test.jsonl"
            events = [{"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": count}}}} for count in (23, 56)]
            path.write_text("\n".join(map(json.dumps, events)) + "\n{", encoding="utf-8")
            self.assertEqual(latest_session_tokens(Path(folder)), 56)

class StreamUsageTests(unittest.TestCase):
    def test_live_counts_the_final_snapshot_once_and_resets_between_calls(self):
        import httpx
        from unittest.mock import patch
        from wisland.live.translate import Translator

        data = [
            {"choices": [{"delta": {"content": "译文"}}]},
            {"usage": {"prompt_tokens": 9, "completion_tokens": 1}},
            {"usage": {"prompt_tokens": 9, "completion_tokens": 3}},
        ]
        def reply(request):
            self.assertTrue(json.loads(request.content)["stream_options"]["include_usage"])
            return httpx.Response(200, text="".join("data: " + json.dumps(d) + "\n\n" for d in data) +
                                  "data: [DONE]\n\n")

        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(reply)) as client:
            db = Path(folder) / "usage.db"
            with patch("wisland.live.translate._client", return_value=client), patch(
                "wisland.live.translate.record_usage", side_effect=lambda b, u: record_usage(b, u, db)
            ):
                translator = Translator({"translate.api_key": "test"})
                self.assertEqual(translator.translate("日本語", []), "译文")
                self.assertEqual(token_totals(db), (9, 3))
                del data[1:]
                translator.translate("日本語", [])
                self.assertEqual(translator.last_usage, {})
                self.assertEqual(token_totals(db), (9, 3))

    def test_text_translation_records_final_usage_chunk_with_empty_choices(self):
        import httpx
        from unittest.mock import patch
        from wisland.services.translate import TranslateService

        def reply(request):
            self.assertTrue(json.loads(request.content)["stream_options"]["include_usage"])
            chunks = [{"choices": [{"delta": {"content": "你好"}}]},
                      {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}}]
            return httpx.Response(200, text="".join("data: " + json.dumps(d) + "\n" for d in chunks) +
                                  "data: [DONE]\n")

        with tempfile.TemporaryDirectory() as folder, httpx.Client(transport=httpx.MockTransport(reply)) as client:
            db = Path(folder) / "usage.db"
            service = TranslateService()
            service._current = 1
            finished = []
            service.finished.connect(lambda rid, out: finished.append((rid, out)))
            with patch.object(service, "_settings", return_value=("test", "https://api.deepseek.com", "model")), \
                 patch.object(service, "_http", return_value=client), patch("wisland.services.translate.ui"), \
                 patch("wisland.services.translate.record_usage", side_effect=lambda b, u: record_usage(b, u, db)):
                service._work(1, "こんにちは")
            self.assertEqual(finished, [(1, "你好")])
            self.assertEqual(token_totals(db), (12, 2))
