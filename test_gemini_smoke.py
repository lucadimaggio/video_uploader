import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


REAL_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

import google.generativeai as genai
from fastapi.testclient import TestClient

import api_gemini
import app as app_module
from api_gemini import GeminiGenerationError


class ResourceExhausted(Exception):
    pass


def _active_file(name="files/test"):
    return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))


def _metadata_response():
    return SimpleNamespace(
        text=json.dumps(
            {
                "yt_title": "Titolo specifico per E-Commerce",
                "yt_description": "Descrizione breve",
                "ig_caption": "Caption breve #ecommerce",
                "fb_description": "Descrizione Facebook",
                "thumbnail_text": "Insight chiave",
            }
        ),
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )


class GeminiSmokeTests(unittest.TestCase):
    def setUp(self):
        api_gemini.GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
        api_gemini.GEMINI_MODEL = "primary-model"
        api_gemini.GEMINI_MAX_ATTEMPTS = 3
        api_gemini.GEMINI_TIMEOUT = 5

    @unittest.skipUnless(REAL_GEMINI_API_KEY, "GEMINI_API_KEY reale non impostata")
    def test_direct_gemini_connection(self):
        genai.configure(api_key=REAL_GEMINI_API_KEY)
        model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-1.5-flash"))
        response = model.generate_content(
            "Rispondi solo 'OK'",
            request_options={"timeout": int(os.environ.get("GEMINI_TIMEOUT", "30"))},
        )
        self.assertIn("OK", response.text.upper())

    def test_retry_succeeds_on_third_attempt(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with patch.object(api_gemini.genai, "configure"), \
                patch.object(api_gemini.genai, "upload_file", return_value=_active_file()), \
                patch.object(api_gemini.genai, "get_file", return_value=_active_file()), \
                patch.object(api_gemini.genai, "delete_file"), \
                patch.object(api_gemini.time, "sleep"), \
                patch.object(api_gemini.genai, "GenerativeModel") as model_cls:
                model_cls.return_value.generate_content.side_effect = [
                    TimeoutError("timeout"),
                    TimeoutError("timeout"),
                    _metadata_response(),
                ]

                result = api_gemini.generate_metadata(video.name, filename="video.mp4")

        self.assertEqual(result["yt_title"], "Titolo specifico per E-Commerce")
        self.assertEqual(model_cls.return_value.generate_content.call_count, 3)

    def test_generate_endpoint_returns_422_when_gemini_fails(self):
        error = GeminiGenerationError(
            "Quota esaurita",
            error_code="rate_limit",
            http_status=429,
            retryable=True,
            fallback_allowed=True,
        )
        with patch.object(app_module, "validate_gemini_config"), \
            patch.object(app_module, "generate_metadata", side_effect=error), \
            patch.object(app_module, "upload_to_r2") as upload_to_r2:
            with TestClient(app_module.app) as client:
                response = client.post(
                    "/generate",
                    headers={"x-api-key": "test-internal-key"},
                    files={"file": ("video.mp4", b"fake-video", "video/mp4")},
                    data={"filename": "video.mp4"},
                )

        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["detail"]["error"], "rate_limit")
        upload_to_r2.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
