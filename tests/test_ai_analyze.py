import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src import ai_analyze

pytestmark = pytest.mark.unit


def _mock_response(body):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestBuildPayload:
    def test_messages_and_model(self):
        payload = ai_analyze.build_payload("model-x", "log text")
        assert payload["model"] == "model-x"
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert "log text" in payload["messages"][1]["content"]

    def test_context_prepended(self):
        payload = ai_analyze.build_payload("m", "log", context="lib1=/mnt/a")
        assert "lib1=/mnt/a" in payload["messages"][1]["content"]

    def test_no_context_no_prefix(self):
        payload = ai_analyze.build_payload("m", "log")
        assert "本次运行配置" not in payload["messages"][1]["content"]

    def test_kwargs_present(self):
        payload = ai_analyze.build_payload("m", "log")
        assert payload["max_tokens"] == 4096
        assert payload["temperature"] == 1.0
        assert payload["top_p"] == 0.95
        assert payload["top_k"] == 64


class TestSendRequest:
    def test_success_returns_json(self):
        resp = _mock_response({"choices": [{"message": {"content": "ok"}}]})
        with patch("urllib.request.urlopen", return_value=resp):
            assert ai_analyze.send_request("http://api", {}) == {
                "choices": [{"message": {"content": "ok"}}]
            }

    def test_urlerror_returns_error_dict(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            result = ai_analyze.send_request("http://api", {})
            assert "error" in result
            assert "down" in result["error"]

    def test_httperror_returns_error_dict(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("http://api", 500, "x", {}, None),
        ):
            result = ai_analyze.send_request("http://api", {})
            assert "error" in result


class TestExtractContent:
    def test_choices_content(self):
        result = {"choices": [{"message": {"content": "hello"}}]}
        assert ai_analyze.extract_content(result) == "hello"

    def test_error_message_fallback(self):
        result = {"error": {"message": "rate limited"}}
        assert ai_analyze.extract_content(result) == "rate limited"

    def test_full_json_fallback(self):
        result = {"unexpected": "shape"}
        assert ai_analyze.extract_content(result) == json.dumps(result, ensure_ascii=False)


class TestAnalyze:
    def test_writes_output(self, tmp_path):
        log_file = tmp_path / "in.log"
        log_file.write_text("some log")
        out_file = tmp_path / "out.md"
        resp = _mock_response({"choices": [{"message": {"content": "# Report"}}]})
        with patch("urllib.request.urlopen", return_value=resp):
            content = ai_analyze.analyze("http://api", "m", str(log_file), str(out_file))
        assert content == "# Report"
        assert out_file.read_text() == "# Report"

    def test_env_system_prompt_used(self, tmp_path, monkeypatch):
        log_file = tmp_path / "in.log"
        log_file.write_text("log")
        out_file = tmp_path / "out.md"
        monkeypatch.setenv("AI_SYSTEM_PROMPT", "CUSTOM PROMPT")
        captured = {}

        def fake_urlopen(req, timeout=180):
            body = json.loads(req.data)
            captured["system"] = body["messages"][0]["content"]
            return _mock_response({"choices": [{"message": {"content": "x"}}]})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ai_analyze.analyze("http://api", "m", str(log_file), str(out_file))
        assert captured["system"] == "CUSTOM PROMPT"

    def test_missing_log_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ai_analyze.analyze("http://api", "m", str(tmp_path / "nope.log"), str(tmp_path / "o"))


class TestMain:
    def test_parses_args_and_runs(self, tmp_path):
        log_file = tmp_path / "in.log"
        log_file.write_text("log")
        out_file = tmp_path / "out.md"
        resp = _mock_response({"choices": [{"message": {"content": "ok"}}]})
        with (
            patch("sys.argv", ["ai_analyze.py", "http://api", "m", str(log_file), str(out_file)]),
            patch("urllib.request.urlopen", return_value=resp),
        ):
            ai_analyze.main()
        assert out_file.read_text() == "ok"
