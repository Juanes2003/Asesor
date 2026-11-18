import hashlib
import hmac
import http.client
import io
import json
import os
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError

import servidor


class WebhookTests(unittest.TestCase):
    phone = "10000001"
    sender = "15550100000"

    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {
            "META_APP_SECRET": "fake-app-secret",
            "META_VERIFY_TOKEN": "fake-verification",
            "WHATSAPP_PHONE_NUMBER_ID": cls.phone,
            "WHATSAPP_TEST_RECIPIENTS": "+" + cls.sender,
            "WHATSAPP_TOKEN": "fake-access-token",
            "GRAPH_API_VERSION": "v25.0",
        })
        cls.env.start()
        cls.addClassCleanup(cls.env.stop)
        cls.enqueue = mock.Mock()
        cls.httpd = servidor.create_server("127.0.0.1", 0, cls.enqueue)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.thread.join, 2)
        cls.addClassCleanup(cls.httpd.shutdown)

    def setUp(self):
        self.enqueue.reset_mock(side_effect=True)
        self.message = {
            "id": "wamid.fake", "from": self.sender, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": "¿Tienen cabestros?"},
        }
        self.value = {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": self.phone},
            "messages": [self.message],
        }
        self.payload = {"object": "whatsapp_business_account", "entry": [
            {"changes": [{"field": "messages", "value": self.value}]},
        ]}

    def request(self, method, target, body=b"", headers=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def post(self, payload=None, signature=None):
        body = json.dumps(self.payload).encode() if payload is None else payload
        digest = hmac.new(b"fake-app-secret", body, hashlib.sha256).hexdigest()
        return self.request("POST", "/webhook", body, {
            "Content-Type": "application/json", "X-Hub-Signature-256": signature or "sha256=" + digest,
        })[0]

    def test_verification_challenge(self):
        path = "/webhook?hub.mode=subscribe&hub.challenge=1234&hub.verify_token="
        self.assertEqual(self.request("GET", path + "fake-verification"), (200, b"1234"))
        self.assertEqual(self.request("GET", path + "wrong")[0], 403)
        self.assertEqual(self.request("GET", "/health"), (200, b"ok"))

    def test_signature_checked_before_json_or_queue(self):
        self.assertEqual(self.post(b"broken", "sha256=" + "0" * 64), 403)
        self.assertEqual(self.post(b"broken"), 400)
        self.enqueue.assert_not_called()

    def test_rejects_oversized_body_before_read(self):
        headers = {"Content-Type": "application/json", "Content-Length": str(servidor.MAX_BODY + 1)}
        self.assertEqual(self.request("POST", "/webhook", headers=headers)[0], 413)
        self.enqueue.assert_not_called()

    def test_rejects_malformed_message_in_signed_batch_before_enqueuing(self):
        self.value["messages"].append(dict(self.message, timestamp="not-a-date"))
        self.assertEqual(self.post(), 400)
        self.enqueue.assert_not_called()

    def test_persists_signed_event_before_acknowledging(self):
        self.assertEqual(self.post(), 200)
        self.enqueue.assert_called_once_with("wamid.fake", self.sender, "¿Tienen cabestros?", int(self.message["timestamp"]))
        self.enqueue.side_effect = OSError("injected")
        self.assertEqual(self.post(), 503)

    def test_ignores_unanswered_events(self):
        self.value["messages"] = [
            dict(self.message, timestamp=str(int(time.time()) - servidor.SERVICE_WINDOW - 1)),
            dict(self.message, type="image"),
            dict(self.message, group_id="fake-group"),
            dict(self.message, **{"from": "15550109999"}),
        ]
        self.assertEqual(self.post(), 200)
        self.value["messages"] = [self.message]
        self.value["metadata"]["phone_number_id"] = "unrelated"
        self.assertEqual(self.post(), 200)
        self.enqueue.assert_not_called()

    def test_status_event_acknowledged(self):
        del self.value["messages"]
        self.value["statuses"] = [{"id": "wamid.fake", "status": "read"}]
        self.assertEqual(self.post(), 200)
        self.enqueue.assert_not_called()

    def test_outbound_verified_once_and_scoped(self):
        with mock.patch.object(servidor, "build_opener") as transport:
            open_request = transport.return_value.open
            open_request.return_value = io.BytesIO(b'{"messages":[{"id":"wamid.sent"}]}')
            with self.assertRaises(ValueError):
                servidor.send_message("15550109999", "Hola")
            transport.assert_not_called()
            servidor.send_message(self.sender, "Respuesta")
            request = open_request.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://graph.facebook.com/v25.0/10000001/messages")
            self.assertEqual(request.get_header("Authorization"), "Bearer fake-access-token")
            self.assertEqual(json.loads(request.data)["to"], self.sender)
            self.assertIsInstance(transport.call_args.args[0], servidor._NoRedirects)
            open_request.assert_called_once()
            open_request.side_effect = HTTPError(request.full_url, 429, "sensitive detail", {}, None)
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                servidor.send_message(self.sender, "Respuesta")
            self.assertEqual(open_request.call_count, 2)

    def test_outbound_ambiguous_result_reported_once(self):
        with mock.patch.object(servidor, "build_opener") as transport:
            transport.return_value.open.side_effect = TimeoutError("sensitive detail")
            with self.assertRaisesRegex(RuntimeError, "^No se pudo verificar"):
                servidor.send_message(self.sender, "Respuesta")
            transport.return_value.open.assert_called_once()

    def test_outbound_redirect_cannot_forward_authorization(self):
        request = servidor.Request("https://graph.facebook.com", data=b"{}", headers={
            "Authorization": "Bearer fake-access-token",
        })
        handler = servidor._NoRedirects()
        self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, "https://example.invalid"))

    def test_allowlist_cannot_be_wildcard(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_TEST_RECIPIENTS": "*"}):
            with self.assertRaises(ValueError):
                servidor.validate_settings()


if __name__ == "__main__":
    unittest.main()
