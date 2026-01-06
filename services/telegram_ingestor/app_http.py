from fastapi import FastAPI, Request
import uvicorn
import logging

app = FastAPI()

@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/notify")
async def notify(request: Request):
    data = await request.json()
    # Aquí puedes procesar la notificación como necesites
    return {"status": "received", "data": data}

# Puedes agregar aquí los endpoints que necesites para la integración

if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    uvicorn.run("app_http:app", host="0.0.0.0", port=8000, reload=False)
