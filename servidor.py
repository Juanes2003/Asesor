"""HTTPS is terminated by the hosting provider in front of this HTTP webhook."""

import hashlib
import hmac
import http.client
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BODY = 1024 * 1024
TEXT_LIMIT = 4096
SERVICE_WINDOW = 24 * 60 * 60


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Falta configurar {name}")
    return value


def _recipients():
    values = _required("WHATSAPP_TEST_RECIPIENTS").split(",")
    numbers = {number.strip().removeprefix("+") for number in values}
    if not all(re.fullmatch(r"[1-9][0-9]{4,14}", number) for number in numbers):
        raise ValueError("Revisa WHATSAPP_TEST_RECIPIENTS. Usa números con indicativo de país.")
    return numbers


def _phone_id():
    value = _required("WHATSAPP_PHONE_NUMBER_ID")
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise ValueError("WHATSAPP_PHONE_NUMBER_ID debe ser el identificador numérico de Meta")
    return value


def _version():
    version = os.environ.get("GRAPH_API_VERSION", "v25.0")
    if not re.fullmatch(r"v[0-9]{1,2}\.[0-9]+", version):
        raise ValueError("GRAPH_API_VERSION debe tener el formato v25.0")
    return version


def validate_settings():
    for name in ("META_APP_SECRET", "META_VERIFY_TOKEN", "WHATSAPP_TOKEN"):
        _required(name)
    _phone_id()
    _recipients()
    _version()


def _values(data, key):
    values = data.get(key)
    if not isinstance(values, list):
        raise ValueError("Se esperaba una lista")
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("Se esperaba un objeto")
        yield value


def _eligible_messages(payload, phone_id, recipients, now):
    if not isinstance(payload, dict):
        raise ValueError("Evento no válido")
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in _values(payload, "entry"):
        for change in _values(entry, "changes"):
            if change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise ValueError("Cambio no válido")
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_id:
                continue
            if value.get("messaging_product") != "whatsapp" or "messages" not in value:
                continue
            for message in _values(value, "messages"):
                sender = message.get("from")
                if not isinstance(sender, str) or sender not in recipients:
                    continue
                if message.get("type") != "text" or message.get("group_id") or value.get("group_id"):
                    continue
                msg_id = message.get("id")
                if not isinstance(msg_id, str) or not 1 <= len(msg_id) <= 512:
                    raise ValueError("Identificador de mensaje no válido")
                timestamp = str(message.get("timestamp", ""))
                if not re.fullmatch(r"[0-9]{1,11}", timestamp):
                    raise ValueError("Fecha de mensaje no válida")
                sent_at = int(timestamp)
                if not now - SERVICE_WINDOW < sent_at <= now + 60:
                    continue
                content = message.get("text")
                text = content.get("body") if isinstance(content, dict) else None
                if not isinstance(text, str) or len(text) > TEXT_LIMIT:
                    raise ValueError("Texto no válido")
                text = text.strip()
                if text:
                    text.encode("utf-8")
                    msg_id.encode("utf-8")
                    yield msg_id, sender, text, sent_at


def create_server(host, port, enqueue):
    secret = _required("META_APP_SECRET").encode()
    verify_token = _required("META_VERIFY_TOKEN").encode()
    phone_id = _phone_id()
    recipients = _recipients()

    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def log_message(self, format, *args):
            # Never log phone numbers, request bodies or the webhook verification query.
            pass

        def reply(self, status, body=b""):
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self):
            target = urlsplit(self.path)
            if target.path == "/health":
                return self.reply(200, b"ok")
            if target.path != "/webhook":
                return self.reply(404)
            try:
                params = parse_qs(target.query, max_num_fields=4)
            except ValueError:
                return self.reply(400)
            provided = params.get("hub.verify_token", [])
            if (params.get("hub.mode") != ["subscribe"] or len(provided) != 1
                    or not hmac.compare_digest(provided[0].encode(), verify_token)):
                return self.reply(403)
            challenge = params.get("hub.challenge", [])
            if len(challenge) != 1 or not 0 < len(challenge[0]) <= 256:
                return self.reply(400)
            return self.reply(200, challenge[0].encode())

        def do_POST(self):
            if urlsplit(self.path).path != "/webhook":
                return self.reply(404)
            if self.headers.get_content_type() != "application/json":
                return self.reply(415)
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                return self.reply(400)
            if not re.fullmatch(r"[0-9]{1,12}", lengths[0]):
                return self.reply(400)
            size = int(lengths[0])
            if size > MAX_BODY:
                return self.reply(413)
            signatures = self.headers.get_all("X-Hub-Signature-256", [])
            if len(signatures) != 1 or not re.fullmatch(r"sha256=[0-9a-f]{64}", signatures[0]):
                return self.reply(403)
            body = self.rfile.read(size)
            if len(body) != size:
                return self.reply(400)
            expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signatures[0]):
                return self.reply(403)
            try:
                payload = json.loads(body.decode("utf-8"))
                messages = list(_eligible_messages(payload, phone_id, recipients, time.time()))
            except (ValueError, RecursionError):
                return self.reply(400)
            try:
                for message in messages:
                    enqueue(*message)
            except Exception:
                # A retry can include previously persisted items. The queue deduplicates by ID.
                return self.reply(503)
            return self.reply(200, b"ok")

    return ThreadingHTTPServer((host, port), Handler)


class _NoRedirects(HTTPRedirectHandler):
    """A webhook response authorizes just one POST to the configured Graph endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_message(recipient: str, text: str) -> None:
    if recipient not in _recipients():
        raise ValueError("Destinatario fuera del grupo de prueba")
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT:
        raise ValueError("Respuesta vacía o superior al límite de WhatsApp")
    url = f"https://graph.facebook.com/{_version()}/{_phone_id()}/messages"
    body = json.dumps({
        "messaging_product": "whatsapp", "recipient_type": "individual",
        "to": recipient, "type": "text", "text": {"body": text, "preview_url": False},
    }).encode()
    token = _required("WHATSAPP_TOKEN")
    request = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        # urllib honors HTTPS_PROXY and NO_PROXY while keeping normal TLS validation.
        with build_opener(_NoRedirects()).open(request, timeout=10) as response:
            result = json.loads(response.read(32769))
        if not isinstance(result, dict) or not any(
            isinstance(item, dict) and item.get("id") for item in result.get("messages", [])
        ):
            raise RuntimeError("WhatsApp no confirmó la aceptación del envío")
    except HTTPError as error:
        error.close()
        raise RuntimeError(f"WhatsApp rechazó el envío (HTTP {error.code})") from None
    except (http.client.HTTPException, OSError, ValueError, TypeError, RecursionError):
        raise RuntimeError("No se pudo verificar el resultado del envío a WhatsApp") from None


if __name__ == "__main__":
    from app import main
    main()
