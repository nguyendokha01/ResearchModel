import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import analysis_transport, answer, config, mcp_server


class ImageRequestTests(unittest.TestCase):
    def _call(self, provider):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "c.png"
            img.write_bytes(b"\x89PNG-bytes")
            seen = {}

            def post(name, req, timeout):
                seen["body"] = json.loads(req.data.decode("utf-8"))
                seen["url"] = req.full_url
                return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

            conf = {"base_url": "http://x/v1", "model": "m", "json_mode": False}
            settings = {"provider": provider, provider: conf, "timeout_seconds": 5}
            with patch.object(answer, "_post_json", post):
                text, _usage = analysis_transport.call(settings, "sys", "user", [img])
        self.assertEqual(text, "{}")
        self.assertTrue(seen["url"].endswith("/chat/completions"))
        parts = seen["body"]["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "user"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_openrouter_image(self):
        self._call("openrouter")

    def test_ollama_image(self):
        self._call("ollama")

    def test_still_rejected_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "c.png"
            img.write_bytes(b"x")
            with self.assertRaises(analysis_transport.ImageInputUnsupported):
                analysis_transport.call({"provider": "deepseek"}, "s", "u", [img])


class RouteTests(unittest.TestCase):
    def _settings(self, yaml_text, task):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "ai.yaml").write_text(yaml_text, encoding="utf-8")
            with patch.object(config, "CONFIG_DIR", Path(tmp)):
                return answer.settings(task)["provider"]

    def test_fallback_and_own_route(self):
        yaml_text = "ai:\n  provider: none\n  tasks:\n    analysis: gemini\n"
        self.assertEqual(self._settings(yaml_text, "figure_reading"), "gemini")
        self.assertEqual(self._settings(yaml_text, "node_card"), "gemini")
        own = yaml_text + "    figure_reading: ollama\n"
        self.assertEqual(self._settings(own, "figure_reading"), "ollama")
        self.assertEqual(self._settings(own, "node_card"), "gemini")


class McpFigureToolTests(unittest.TestCase):
    def test_tools_registered(self):
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertTrue({"get_figure_task", "submit_figure_reading", "list_pending_figures"} <= names)

    def test_image_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "c.png"
            img.write_bytes(b"abc")
            block = mcp_server._image_content(str(img))[0]
            self.assertEqual((block["type"], block["mimeType"]), ("image", "image/png"))
            self.assertEqual(mcp_server._image_content(str(img), limit=1), [])
            self.assertEqual(mcp_server._image_content(None), [])


if __name__ == "__main__":
    unittest.main()
