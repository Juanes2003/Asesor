"""Comprobación HTTP de la tienda con los tres proveedores externos simulados."""

import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app
from estado import Estado
import respuestas
import servidor


CATALOGO = """Producto,Precio COP,Estado,Categoría,Foto,Descripción,Código
Rienda Doble Costura,145000,Disponible,Accesorios,,Doble costura.,2
Referencia interna,1,Oculto,Accesorios,,,9
""".encode()
CARRITO = """Hola, quiero confirmar un pedido en Riendas Diego.

Nombre: Cliente De Prueba
Ciudad: Bello

PEDIDO
• 2 x Rienda Doble Costura — $ 2

Subtotal: $ 2
Envío: por confirmar

Notas: cliente@example.invalid

¿Me confirman disponibilidad, envío y forma de pago?"""


class FlujoCompleto(unittest.TestCase):
    def setUp(self):
        self.phone_id = "555001"
        self.recipient = "15550100000"
        self.enterContext(patch.dict(os.environ, {
            "META_APP_SECRET": "firma-inventada", "META_VERIFY_TOKEN": "token-prueba",
            "WHATSAPP_TOKEN": "token-inventado", "GRAPH_API_VERSION": "v25.0",
            "WHATSAPP_PHONE_NUMBER_ID": self.phone_id,
            "WHATSAPP_TEST_RECIPIENTS": self.recipient,
            "GEMINI_API_KEY": "clave-inventada", "GEMINI_MODEL": respuestas.DEFAULT_MODEL,
        }))
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "prueba.sqlite3"
        self.state = Estado(self.db, monthly_limit=4)
        server = servidor.create_server("127.0.0.1", 0, self.state.enqueue)
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)

        self.gemini_calls = []
        self.enterContext(patch.object(respuestas, "urlopen", self.google))
        opener = self.enterContext(patch.object(servidor, "build_opener"))
        self.graph = opener.return_value.open
        self.graph.side_effect = lambda *args, **kwargs: io.BytesIO(b'{"messages":[{"id":"wamid.salida"}]}')

    def google(self, request, timeout):
        if isinstance(request, str):
            self.assertEqual(request, respuestas.CATALOG_URL)
            return io.BytesIO(CATALOGO)
        self.assertEqual(request.get_header("X-goog-api-key"), "clave-inventada")
        self.assertTrue(request.full_url.endswith(":generateContent"))
        data = json.loads(request.data)
        question = json.loads(data["contents"][0]["parts"][0]["text"])
        self.gemini_calls.append(question)
        self.assertEqual(len(question["catalogo"]), 1)
        product = question["catalogo"][0]
        amount = f'{product["precio_COP"]:,}'.replace(",", ".")
        answer = f'{product["producto"]} cuesta $ {amount} COP y está disponible.'
        return io.BytesIO(json.dumps({"candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": answer}]}}
        ]}).encode())

    def post(self, message_id=None, question=None):
        value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": self.phone_id}}
        if message_id is None:
            value["statuses"] = [{"id": "wamid.salida", "status": "delivered"}]
        else:
            value["messages"] = [{"id": message_id, "from": self.recipient,
                                  "timestamp": str(int(time.time())), "type": "text",
                                  "text": {"body": question}}]
        body = json.dumps({
            "object": "whatsapp_business_account", "entry": [
                {"id": "cuenta-de-prueba", "changes": [{"field": "messages", "value": value}]}
            ]
        }).encode()
        digest = hmac.new(b"firma-inventada", body, hashlib.sha256).hexdigest()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", "/webhook", body=body, headers={
                "Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + digest,
            })
            result = connection.getresponse()
            self.assertEqual((result.status, result.read()), (200, b"ok"))
        finally:
            connection.close()

    def test_carrito_persistencia_contexto_y_reentrega(self):
        self.post("wamid.carrito", CARRITO)
        self.post("wamid.carrito", CARRITO)
        self.post()  # Las notificaciones de entrega no originan respuestas.

        # Una nueva instancia procesa el mensaje que el webhook ya confirmó.
        worker_state = Estado(self.db, monthly_limit=4)
        self.assertTrue(app.process_next(worker_state))
        self.assertFalse(app.process_next(worker_state))
        self.assertEqual(self.gemini_calls, [])
        calls = self.graph.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertIn("$ 290.000 COP", json.loads(calls[0].args[0].data)["text"]["body"])

        self.post("wamid.pregunta", "¿Qué precio tiene la Rienda Doble Costura?")
        self.post("wamid.seguimiento", "¿Y el envío a Cali?")
        self.assertTrue(app.process_next(worker_state))
        self.assertTrue(app.process_next(worker_state))
        self.post("wamid.pregunta", "¿Qué precio tiene la Rienda Doble Costura?")
        self.assertFalse(app.process_next(worker_state))

        self.assertEqual(len(self.gemini_calls), 2)
        first, followup = self.gemini_calls
        self.assertEqual(first["catalogo"][0]["precio_COP"], 145000)
        self.assertNotIn("cliente@example.invalid", str(self.gemini_calls))
        self.assertNotIn("Cliente De Prueba", str(self.gemini_calls))
        self.assertNotIn(self.recipient, str(self.gemini_calls))
        self.assertNotIn("Referencia interna", str(self.gemini_calls))
        self.assertIn("doble costura", followup["dialogo"][-1]["consulta"])
        self.assertIn("cali", followup["consulta"])

        self.assertEqual(len(calls), 3)
        for call in calls:
            request = call.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, "https://graph.facebook.com/v25.0/555001/messages")
            self.assertEqual(request.get_header("Authorization"), "Bearer token-inventado")
            outgoing = json.loads(request.data)
            self.assertEqual(outgoing["messaging_product"], "whatsapp")
            self.assertEqual(outgoing["to"], self.recipient)
            self.assertEqual(outgoing["type"], "text")
            self.assertNotIn("cliente@example.invalid", outgoing["text"]["body"])
        self.assertIn("$ 145.000 COP", json.loads(calls[1].args[0].data)["text"]["body"])


if __name__ == "__main__":
    unittest.main()
