"""
REST Adapter - Bridge entre SuperNEXUS y OpenClaw Gateway
Puerto: 18790
Traduce peticiones de Nexus (/api/chat) al formato OpenClaw (chat completions)
y las envía al gateway de OpenClaw en 18789 con autenticación.
"""

import logging
import os
import httpx
from dotenv import load_dotenv
from aiohttp import web

load_dotenv(encoding="utf-8-sig")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [rest-adapter] %(levelname)s: %(message)s")
logger = logging.getLogger("rest_adapter")

OPENCLAW_URL  = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_TOKEN = os.getenv("OPENCLAW_TOKEN", "")
if not OPENCLAW_TOKEN:
    logger.warning("OPENCLAW_TOKEN env var not set — OpenClaw gateway will reject requests.")
OPENCLAW_MODEL = os.getenv("OPENCLAW_MODEL", "ollama/qwen3.6:latest")
ADAPTER_PORT   = int(os.getenv("REST_ADAPTER_PORT", "18790"))


async def health(request):
    return web.json_response({"status": "online", "service": "rest_adapter", "port": ADAPTER_PORT})


async def chat(request):
    """
    Recibe petición estilo Nexus: {"message": "...", ...}
    La traduce al formato OpenClaw chat completions y reenvía.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    message = body.get("message") or body.get("content") or ""
    if not message:
        return web.json_response({"error": "No message provided"}, status=400)

    # Formato OpenClaw (compatible OpenAI chat completions)
    openclaw_payload = {
        "model": OPENCLAW_MODEL,
        "messages": [{"role": "user", "content": message}],
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {OPENCLAW_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{OPENCLAW_URL}/v1/chat/completions",
                json=openclaw_payload,
                headers=headers,
            )

        if r.status_code == 200:
            data = r.json()
            reply = data["choices"][0]["message"]["content"]
            logger.info(f"OpenClaw respondió OK ({len(reply)} chars)")
            return web.json_response({"reply": reply, "source": "openclaw", "model": OPENCLAW_MODEL})
        else:
            logger.error(f"OpenClaw error {r.status_code}: {r.text[:200]}")
            return web.json_response(
                {"error": f"OpenClaw HTTP {r.status_code}", "detail": r.text[:500]},
                status=502,
            )

    except httpx.ConnectError:
        logger.error("No se puede conectar al OpenClaw gateway en 18789")
        return web.json_response({"error": "OpenClaw gateway no disponible en 18789"}, status=503)
    except Exception as e:
        logger.error(f"Error inesperado: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def completions(request):
    """
    También acepta peticiones en formato OpenAI directamente
    y las reenvía al OpenClaw gateway con el token correcto.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    headers = {
        "Authorization": f"Bearer {OPENCLAW_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{OPENCLAW_URL}/v1/chat/completions",
                json=body,
                headers=headers,
            )
        return web.Response(
            body=r.content,
            status=r.status_code,
            content_type="application/json",
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def main():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/",       health)
    app.router.add_post("/api/chat",             chat)
    app.router.add_post("/v1/chat/completions",  completions)

    logger.info(f"REST Adapter iniciando en puerto {ADAPTER_PORT}")
    logger.info(f"Conectando a OpenClaw gateway: {OPENCLAW_URL}")
    web.run_app(app, host="127.0.0.1", port=ADAPTER_PORT, print=None)


if __name__ == "__main__":
    main()
