import os
import httpx

TELEGRAM_INGESTOR_URL = os.getenv("TELEGRAM_INGESTOR_URL", "http://localhost:8000")
CHAT_ID = os.getenv("TG_TEST_CHAT_ID","-5250557024")  # Pon aquí el chat_id de prueba o usa una variable de entorno
MENSAJE = "🔔 Prueba directa de notificación desde el contenedor orchestrator."

def main():
    if not CHAT_ID:
        print("Falta TG_TEST_CHAT_ID en el entorno.")
        return
    url = f"{TELEGRAM_INGESTOR_URL}/notify"
    payload = {"chat_id": CHAT_ID, "message": MENSAJE}
    try:
        resp = httpx.post(url, json=payload, timeout=10)
        print("Status:", resp.status_code)
        print("Response:", resp.text)
    except Exception as e:
        print("Error al enviar:", e)

if __name__ == "__main__":
    main()
