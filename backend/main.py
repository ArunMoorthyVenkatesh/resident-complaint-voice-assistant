"""
main.py — STE BuildCare backend

FastAPI server that powers the Maya voice complaint system.

Endpoints:
  - /gemini-ws        : the voice complaint line — proxies browser mic audio to
                         Gemini Live (see gemini_live.py) and streams the spoken reply back
  - /complaints       : read-only DynamoDB view, used by the admin dashboard
  - /complaints/patterns : cross-complaint pattern detection (see patterns.py)
  - /complaints/{id}/status : update a complaint's status
  - /complaints/clear : wipe all complaint data (requires ?confirm=true)
"""

import os
from dotenv import load_dotenv
# Load .env from the same directory as this file, regardless of where the server is started
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import logging
from dynamodb import init_table, get_all_complaints, clear_all_complaints, update_status, VALID_STATUSES
from patterns import detect_patterns
from gemini_live import handle_gemini_ws

import asyncio
from fastapi import FastAPI, HTTPException, Request, WebSocket, Body
from botocore.exceptions import ClientError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

API_KEY = os.getenv("API_KEY")

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


# --- FastAPI App Setup ---
app = FastAPI(title="Building Complaint AI API", version="2.0.0")


@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    if request.method == "OPTIONS" or \
       request.url.path in ["/", "/health", "/docs", "/redoc", "/openapi.json"] or \
       request.url.path.startswith("/ws"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if api_key.startswith("Bearer "):
        api_key = api_key[7:]

    if not api_key or api_key != API_KEY:
        logger.warning(f"Unauthorized from {request.client.host if request.client else 'unknown'}")
        return JSONResponse(
            status_code=401,
            content={"error": "UNAUTHORIZED", "message": "Valid X-API-Key header required."},
        )
    return await call_next(request)


@app.on_event("startup")
async def startup_event():
    init_table()


@app.get("/health")
async def health_check():
    from dynamodb import _get_table
    try:
        _get_table().load()
        return {"status": "ok", "dynamodb": "connected"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "degraded", "dynamodb": "unavailable"})


origins = [
    "http://localhost:5173",
    "http://localhost:5174",
    "http://localhost:5175",
    "http://localhost:5176",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "https://18-143-155-12.sslip.io",
    "https://admin.18-143-155-12.sslip.io",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


# --- Gemini Live WebSocket ---
@app.websocket("/gemini-ws")
async def gemini_websocket(websocket: WebSocket, api_key: str = ""):
    if not api_key or api_key != API_KEY:
        await websocket.close(code=4001)
        return
    await handle_gemini_ws(websocket)


# --- Complaints endpoints ---
@app.get("/complaints")
async def list_complaints():
    try:
        complaints = get_all_complaints()
        return {"complaints": complaints}
    except Exception as e:
        logger.error(f"Failed to fetch complaints: {e}")
        return {"complaints": [], "warning": "DynamoDB not configured."}


@app.get("/complaints/patterns")
async def complaint_patterns():
    """Cross-complaint pattern detection for the admin dashboard — see patterns.py."""
    try:
        alerts = detect_patterns(get_all_complaints())
        return {"alerts": alerts}
    except Exception as e:
        logger.error(f"Failed to compute complaint patterns: {e}")
        return {"alerts": [], "warning": "Pattern detection unavailable."}


@app.patch("/complaints/{complaint_id}/status")
async def update_complaint_status(complaint_id: str, status: str = Body(..., embed=True)):
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {VALID_STATUSES}")
    try:
        update_status(complaint_id, status)
        return {"complaint_id": complaint_id, "status": status}
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HTTPException(status_code=404, detail="Complaint not found")
        logger.error(f"Failed to update status for {complaint_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update status")


@app.delete("/complaints/clear")
async def clear_complaints(confirm: bool = False):
    """Irreversibly deletes every complaint. Requires ?confirm=true to guard against
    an accidental call — there is no undo once the DynamoDB items are gone."""
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="This permanently deletes all complaints. Pass ?confirm=true to proceed.",
        )
    try:
        clear_all_complaints()
    except Exception as e:
        logger.error(f"Failed to clear complaints: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear complaints")
    return {"message": "All complaints cleared."}


# --- Root ---
@app.get("/")
async def read_root():
    return {
        "message": "Building Complaint AI API",
        "version": "2.0.0",
        "assistant": "Maya — Building Supervisor Assistant",
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# Lambda entrypoint — wraps the FastAPI app for AWS Lambda + API Gateway HTTP API
from mangum import Mangum
_stage = os.getenv("STAGE", "prod")
lambda_handler = Mangum(app, lifespan="off", api_gateway_base_path=f"/{_stage}")
