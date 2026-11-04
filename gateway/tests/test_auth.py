from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from gateway.auth import find_app_by_key, get_client_app

ETATCIVIL_KEY = "etatcivil-test-key"
COURRIER_KEY = "courrier-test-key"


class ClientAppRegistryTests(SimpleTestCase):
    def test_find_app_by_valid_key(self):
        app = find_app_by_key(ETATCIVIL_KEY)
        self.assertEqual(app.code, "ETATCIVIL")
        self.assertEqual(app.daily_quota, 100)

    def test_find_app_by_unknown_key_returns_none(self):
        self.assertIsNone(find_app_by_key("unknown"))

    def test_get_client_app_by_code(self):
        self.assertEqual(get_client_app("COURRIER").daily_quota, 5)
        self.assertIsNone(get_client_app("NOPE"))

    @override_settings(CLIENT_APPS={"TEST": {"api_key": "k-test", "daily_quota": 7}})
    def test_registry_reads_settings(self):
        self.assertEqual(find_app_by_key("k-test").code, "TEST")
        self.assertIsNone(find_app_by_key(ETATCIVIL_KEY))


class ApiKeyAuthenticationTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_missing_key_is_rejected_with_401(self):
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_invalid_key_is_rejected_with_401(self):
        response = self.client.get("/api/me", HTTP_X_API_KEY="bad-key")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_failed")
        self.assertIn("message", response.json()["error"])

    def test_valid_key_identifies_each_app(self):
        for key, code, quota in [(ETATCIVIL_KEY, "ETATCIVIL", 100), (COURRIER_KEY, "COURRIER", 5)]:
            with self.subTest(code=code):
                response = self.client.get("/api/me", HTTP_X_API_KEY=key)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"code": code, "daily_quota": quota})


class ApiDocumentationTests(SimpleTestCase):
    def test_swagger_ui_is_public(self):
        self.assertEqual(self.client.get("/api/docs").status_code, 200)

    def test_schema_declares_api_key_scheme(self):
        response = self.client.get("/api/schema", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 200)
        scheme = response.json()["components"]["securitySchemes"]["ApiKeyAuth"]
        self.assertEqual(scheme, {"type": "apiKey", "in": "header", "name": "X-API-Key"})
