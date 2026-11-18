"""Asesor de WhatsApp de Riendas Diego. Ejecutar con Python 3.12 o posterior."""
import argparse
from getpass import getpass
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
import threading

from estado import Estado
from respuestas import generate_reply
from servidor import create_server, send_message, validate_settings

LOG = logging.getLogger("asesor")
FIELDS = {
    "GEMINI_API_KEY": "Clave del proyecto gratuito de Google AI Studio",
    "META_APP_SECRET": "Secreto de la aplicación de Meta (App Secret)",
    "WHATSAPP_TOKEN": "Token de acceso del número de prueba de WhatsApp",
    "WHATSAPP_PHONE_NUMBER_ID": "Identificador del número de prueba (Phone Number ID)",
    "WHATSAPP_TEST_RECIPIENTS": "Destinatarios de prueba verificados, con indicativo de país y separados por comas",
}


def config_path():
    return Path.home() / ".config" / "riendas-diego" / "prueba.json"


def load_config():
    path = config_path()
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
        for key in (*FIELDS, "META_VERIFY_TOKEN"):
            if key in values:
                os.environ.setdefault(key, values[key])


def configure():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("Abre una terminal privada e interactiva para configurar la prueba.")
    path = config_path()
    if path.exists():
        raise ValueError(f"Ya existe la configuración en {path}. Puedes editarla en tu equipo.")
    values = {}
    for name, prompt in FIELDS.items():
        value = getpass(prompt + ": ").strip()
        if not value:
            raise ValueError(f"Falta {name}.")
        values[name] = value
    values["META_VERIFY_TOKEN"] = secrets.token_urlsafe(32)
    os.environ.update(values)
    validate_settings()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            json.dump(values, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            os.link(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    print("Configuración guardada. Copia este token en el campo Verify token del webhook de Meta:")
    print(values["META_VERIFY_TOKEN"])


def process_next(state, generate=generate_reply, send=send_message):
    job = state.claim()
    if job is None:
        return False
    # Antes de reservar, un reinicio puede recuperar el trabajo sin duplicar un envío.
    try:
        reply = generate(job.question, state.history(job))
    except Exception as exc:
        LOG.error("Fallo al preparar la respuesta (%s).", type(exc).__name__)
        state.abandon(job)
        return True
    if not state.reserve_send(job, reply):
        return True
    try:
        send(job.sender, reply)
    except Exception as exc:
        LOG.error("Meta no confirmó el envío (%s). Revisa el panel de Meta.", type(exc).__name__)
        state.finish(job, delivered=False)
    else:
        state.finish(job, delivered=True)
    return True


def listen():
    validate_settings()
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("Falta GEMINI_API_KEY.")
    data_dir = Path(os.getenv("DATA_DIR", str(Path.home() / ".local/share/riendas-diego")))
    limit = int(os.getenv("MONTHLY_REPLY_LIMIT", "60"))
    state = Estado(data_dir / "asesor.sqlite3", monthly_limit=limit)
    stop = threading.Event()
    work = threading.Event()

    def enqueue(*message):
        if state.enqueue(*message):
            work.set()

    def consume():
        while not stop.is_set():
            work.clear()
            try:
                if process_next(state):
                    continue
            except Exception as exc:
                LOG.error("La cola necesita atención (%s).", type(exc).__name__)
            work.wait(timeout=30)

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    with create_server(host, port, enqueue) as server:
        worker = threading.Thread(target=consume, name="respuestas", daemon=True)
        worker.start()
        # handle_request permite cerrar desde el hilo principal al recibir SIGTERM.
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        LOG.info("Webhook de prueba listo en %s:%d. Tope mensual: %d.", host, port, limit)
        server.timeout = 1
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            stop.set()
            work.set()
            worker.join(timeout=45)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configurar", action="store_true", help="Guardar credenciales de prueba en este equipo.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.configurar:
            configure()
        else:
            load_config()
            listen()
    except (ValueError, OSError) as exc:
        sys.exit(f"No se pudo iniciar el asesor: {exc}")


if __name__ == "__main__":
    main()
