# Asesor de WhatsApp de Riendas Diego

Asesor de prueba en español para el catálogo de Riendas Diego, en Bello, Antioquia. Responde a mensajes entrantes de texto de los teléfonos de prueba autorizados. Consulta la misma hoja pública que usa la tienda para los precios en COP y la disponibilidad. Si recibe un carrito preparado por la web, recalcula el subtotal de las piezas disponibles. Para consultas comerciales utiliza Gemini y conserva hasta cuatro intercambios recientes como contexto.

## Probar el programa sin cuentas

Descomprime el ZIP, abre una terminal en la carpeta que contiene `app.py` y ejecuta el comando con Python 3.12 o superior.

```sh
python3 -m unittest discover -v
```

Las pruebas simulan Google y Meta. Una de ellas recorre un pedido del carrito, un mensaje duplicado y dos preguntas relacionadas a través del webhook HTTP, la cola SQLite y el envío de respuesta.

## Activar un número de prueba

1. En Google AI Studio, crea o selecciona un proyecto que muestre **Free Tier** y obtén su clave. La suscripción personal a Gemini se factura por separado. [Claves de Google AI Studio](https://aistudio.google.com/apikey) · [Facturación de la API](https://ai.google.dev/gemini-api/docs/billing)
2. En Meta for Developers, abre o crea una app con el caso de uso de WhatsApp. En la configuración de la API selecciona el **número de prueba** de Meta, añade tu propio teléfono como destinatario y completa su verificación. Necesitarás el **Phone Number ID** del remitente, el token de acceso con permiso `whatsapp_business_messaging` y el secreto de la app. El token temporal tiene fecha de caducidad. [Panel de aplicaciones](https://developers.facebook.com/apps/) · [Guía oficial de WhatsApp](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started/)
3. En una terminal privada, ejecuta los dos comandos siguientes, uno después del otro. El primero lee las claves sin mostrarlas, las guarda para tu usuario y genera el token de verificación del webhook. El segundo inicia el asesor.

```sh
python3 app.py --configurar
python3 app.py
```

En Windows puedes usar `py -3.12` en lugar de `python3`. La configuración queda en `~/.config/riendas-diego/prueba.json`. Si caduca el token temporal, actualiza el campo `WHATSAPP_TOKEN` en ese archivo desde un editor local y reinicia el proceso. Mantén las claves fuera de Git, del chat y de la hoja de productos.

4. Meta necesita una dirección HTTPS que pueda alcanzar. Para una prueba desde tu equipo, instala la herramienta oficial `cloudflared`. Con el asesor ejecutándose, abre otra terminal y lanza el túnel. La herramienta muestra una URL temporal. Añade `/webhook` al final para obtener la **Callback URL**. Mantén ambas terminales abiertas durante la prueba. [Instalación de cloudflared](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) · [Túneles temporales gratuitos](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

```sh
cloudflared tunnel --url http://127.0.0.1:8080
```

5. En la configuración de Webhooks de WhatsApp en Meta, guarda la **Callback URL** y el **Verify token** que generó el paso 3. Suscribe el campo **messages** del objeto **whatsapp_business_account**. Comprueba también que la app esté suscrita a la cuenta de WhatsApp Business (WABA) que contiene el número de prueba. [Webhooks de WhatsApp](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-whatsapp/)
6. Si necesitas iniciar el chat de prueba, usa la plantilla de muestra de Meta para tu propio destinatario verificado. Desde ese WhatsApp responde: «¿Qué diferencia hay entre la rienda trenzada y la de mosquetón? ¿Envían a Medellín?». Comprueba que llega una respuesta a ambas preguntas. Continúa con «¿Cuánto costarían dos de la primera?» y contrasta el subtotal con la hoja. Para comprobar que intervino Gemini, busca en el registro del servidor la confirmación de respuesta del modelo. Un mensaje que te remita al equipo puede indicar que el modelo está temporalmente indisponible.

## Límites de la prueba

El tope inicial es de **60 intentos de respuesta por mes** para todo el bot. La cola registra cada intento antes de llamar a Meta. Si esa llamada falla y se desconoce si llegó el mensaje, el bot evita un segundo envío. Cerca del cambio de mes se computa el intento en ambos meses. Para pausar todos los envíos establece `MONTHLY_REPLY_LIMIT=0` y reinicia. Las alertas de precios de una cuenta y otros mensajes enviados con el mismo número se gestionan por separado.

Para probar sin cargos adicionales usa el número de prueba de Meta, un proyecto gratuito de Gemini y tu propio equipo. El programa no puede comprobar el plan de facturación de tu cuenta de Google. La lista `WHATSAPP_TEST_RECIPIENTS` limita quién puede recibir respuestas. La cola borra el texto y el identificador del remitente de los eventos caducados al procesar o hacer limpieza periódica, después de un máximo aproximado de 24 horas mientras está en ejecución. Los contadores mensuales permanecen en SQLite.

El asesor trata localmente los carritos y varias consultas que incluyen datos de contacto, pago o entrega. Para las demás reconstruye la consulta con vocabulario comercial y datos públicos del catálogo antes de enviarla a Gemini. La prueba está pensada para consultas ficticias de productos. El nivel gratuito de la API de Google tiene condiciones de tratamiento de datos específicas. [Condiciones de Gemini](https://ai.google.dev/gemini-api/terms)

## Ejecutar en un servidor existente

Para operación continua usa una URL HTTPS estable y una sola base SQLite persistente por número. Coloca el backend en un servidor de aplicaciones. La tienda puede seguir en GitHub Pages. Incluimos un Dockerfile. Desde esta carpeta, `docker build -t asesor-riendas .` construye la imagen. Configura con el gestor de secretos del servidor las variables de más abajo y monta un volumen escribible en `/data`. El contenedor escucha en el puerto 8080 y expone `GET /health` y `GET/POST /webhook`. Una instalación nueva debe reutilizar la base de datos para conservar el presupuesto y la deduplicación.

| Variable | Uso |
| --- | --- |
| GEMINI_API_KEY | Clave de Google AI Studio de un proyecto en nivel gratuito |
| META_APP_SECRET | Secreto de la aplicación de Meta que firma los eventos |
| META_VERIFY_TOKEN | Token privado elegido por el operador para verificar el webhook |
| WHATSAPP_PHONE_NUMBER_ID | Identificador del número de prueba de la API |
| WHATSAPP_TOKEN | Token de Meta para enviar como ese número |
| WHATSAPP_TEST_RECIPIENTS | Teléfonos verificados con indicativo internacional, separados por comas |
| DATA_DIR | Carpeta persistente de SQLite. En Docker, `/data` |
| MONTHLY_REPLY_LIMIT | Por defecto 60. Valores admitidos de 0 a 1000 |
| GEMINI_MODEL | Por defecto `gemini-3.5-flash-lite`. Verifica que el modelo elegido esté incluido en el nivel gratuito |
| GRAPH_API_VERSION | Por defecto `v25.0` |
| HOST, PORT | Por defecto `127.0.0.1` y `8080` en ejecución local |

Los tokens, los teléfonos personales y los datos de conversaciones se mantienen fuera del paquete de distribución. Si una persona solicita atención, el piloto la dirige al WhatsApp de la tienda indicado en su sitio.
