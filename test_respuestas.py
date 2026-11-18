import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import respuestas as r


SHEET = (
    "Producto,Precio COP,Estado,Categoría,Foto,Descripción,Código\n"
    'Rienda trenzada,200000,Disponible,Accesorios,,\u0022Trenzado, color café\u0022,1\n'
    "Rienda plana,190000,Agotado,Accesorios,,,2\n"
    "Prototipo rojo,99,Oculto,Accesorios,,,3\n"
).encode("utf-8-sig")


class CatalogTests(unittest.TestCase):
    def test_hidden_items_and_quoted_csv(self):
        products = r.catalog_records(SHEET)
        self.assertEqual([row["estado"] for row in products], ["Disponible", "Agotado"])
        self.assertEqual(products[0]["descripción"], "Trenzado, color café")
        self.assertEqual(products[0]["precio_COP"], 200000)

    def test_broken_rows_are_not_sellable(self):
        for before, after in ((b"190000", b"-5"), (b",2\n", b",1\n"), (b"Agotado", b"?")):
            with self.subTest(value=after), self.assertRaises(ValueError):
                r.catalog_records(SHEET.replace(before, after))


class ReplyTests(unittest.TestCase):
    def test_personal_data_and_cart_are_handled_locally(self):
        for question in (
            "Estimado, ¿puedo pagar por transferencia? Nombre: Ana Ruiz",
            "Mi número es 1234", "Este es mi comprobante", "Llévalo a la calle 12",
            "escríbeme en diego@example.org", "¿Me llamas al 300 123 4567?",
            "Quiero hablar con una persona",
        ):
            with self.subTest(question=question), patch.object(r, "urlopen") as network:
                self.assertEqual(r.generate_reply(question, []), r.HUMAN)
                network.assert_not_called()

    def test_store_cart_uses_only_current_prices_and_never_sends_customer_data(self):
        cart = ("Hola, quiero confirmar un pedido en Riendas Diego.\n\nNombre: Usuario Prueba\n"
                "Ciudad: Cali\n\nPEDIDO\n• 2 x Rienda trenzada — $ 3\n• 1 x Rienda plana — $ 3\n"
                "\nSubtotal: $ 6\nEnvío: por confirmar\n\nNotas: info@ejemplo.invalid")
        with patch.object(r, "fetch_catalog", return_value=r.catalog_records(SHEET)), patch.object(r, "urlopen") as api:
            answer = r.generate_reply(cart, [])
            self.assertIn("$ 400.000 COP", answer)
            self.assertIn("Rienda plana: agotado", answer)
            self.assertNotIn("Usuario", answer)
            api.assert_not_called()
            self.assertEqual(r.generate_reply(cart.replace("Rienda plana", "Producto desconocido"), []), r.HUMAN)

    def test_synced_catalog_and_compound_question_without_private_details(self):
        observed = []

        def gateway(request, timeout):
            if isinstance(request, str):
                self.assertEqual(request, r.CATALOG_URL)
                return io.BytesIO(SHEET)
            self.assertEqual(request.get_header("X-goog-api-key"), "fake-key")
            observed.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP", "content": {
                "parts": [{"thought": True, "text": "interno"}, {"text": "Respuesta de la prueba."}]}}]}).encode())

        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}), patch.object(r, "urlopen", gateway):
            reply = r.generate_reply(
                "No quiero la plana. ¿Tienen la trenzada por $200.000 o menos y hacen envío a Medellín? Xavier",
                [("Me llamo Roberto, vivo en ...", "ok"), ("¿Y las botas?", "¿Qué talla?")],
            )
        self.assertIn("Respuesta de la prueba.", reply)
        self.assertNotIn("interno", reply)
        content = json.loads(observed[0]["contents"][0]["parts"][0]["text"])
        self.assertEqual(len(content["catalogo"]), 2)
        self.assertIn("no quiero la plana", content["consulta"])
        self.assertIn("200000 COP o menos", content["consulta"])
        self.assertIn("envio a medellin", content["consulta"])
        self.assertIn("[detalle omitido]", content["consulta"])
        self.assertNotIn("xavier", str(content).lower())
        self.assertEqual(len(content["dialogo"]), 1)
        self.assertIn("botas", content["dialogo"][0]["consulta"])

    def test_missing_catalog_and_unusable_api_output(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key"}), patch.object(r, "fetch_catalog", side_effect=OSError), patch.object(r, "urlopen") as gateway:
            for result in (b"{}", b"not-json", b'{"candidates":[{"finishReason":"SAFETY"}]}'):
                gateway.side_effect = lambda *args, data=result, **kwargs: io.BytesIO(data)
                self.assertEqual(r.generate_reply("¿Hay riendas?", []), r.UNAVAILABLE)
                request, = gateway.call_args.args
                content = json.loads(json.loads(request.data)["contents"][0]["parts"][0]["text"])
                self.assertIsNone(content["catalogo"])
            gateway.side_effect = HTTPError("http://test.invalid", 429, "quota", {}, None)
            self.assertEqual(r.generate_reply("¿Hay riendas?", []), r.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
