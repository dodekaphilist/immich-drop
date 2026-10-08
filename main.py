"""Entrypoint: reads host/port from the environment and starts Uvicorn.
Set RELOAD=true for auto-reload while developing (off by default, also in Docker).
"""
import os
import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    # Load .env for host/port only; app config loads in app.config
    try:
        load_dotenv()
    except Exception:
        pass
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    reload = os.getenv("RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"}
    uvicorn.run("app.app:app", host=host, port=port, reload=reload)
