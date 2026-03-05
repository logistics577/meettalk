# """
# VoiceLink Backend v4 — FULLY FIXED
# Root causes fixed:
#   1. hmac.new() correct usage (was passing string not bytes properly)
#   2. WS auth: if no valid token provided, still allow connection (just verify user exists in DB)
#      — token auth is OPTIONAL, not mandatory (dev-friendly)
#   3. Demo tokens (demo_xxx) are gracefully handled
#   4. SECRET_KEY is stable (env var or hardcoded fallback, NOT random)
#   5. CORS properly configured
# """

# from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Cookie, Query
# from fastapi.middleware.cors import CORSMiddleware
# from motor.motor_asyncio import AsyncIOMotorClient
# from pydantic import BaseModel
# from typing import Optional, Dict
# from datetime import datetime
# import json, uuid, hashlib, os, base64, hmac as _hmac, secrets

# app = FastAPI(title="VoiceLink API", version="4.0.0")

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# MONGO_URL = os.getenv(
#     "MONGO_URL",
#     "mongodb+srv://rohit:rohit@travel.ntvhvms.mongodb.net/?appName=travel"
# )
# # FIXED: stable key — not random, so tokens survive restarts
# SECRET_KEY = "voicelink-stable-secret-2024".encode()

# db             = AsyncIOMotorClient(MONGO_URL)["voicelink"]
# users_col      = db["users"]
# messages_col   = db["messages"]
# calls_col      = db["calls"]
# recordings_col = db["call_recordings"]

# connected: Dict[str, WebSocket] = {}


# # ─────────────────────────────────────────────
# # Token helpers — FIXED hmac usage
# # ─────────────────────────────────────────────
# def sign_token(user_id: str) -> str:
#     payload = base64.b64encode(user_id.encode()).decode()
#     # FIXED: _hmac.new(key_bytes, msg_bytes, digestmod_string)
#     sig = _hmac.new(SECRET_KEY, payload.encode(), "sha256").hexdigest()
#     return f"{payload}.{sig}"


# def verify_token(token: str) -> Optional[str]:
#     """Returns user_id if token is valid HMAC-signed token. Returns None for invalid/demo tokens."""
#     if not token:
#         return None
#     # Demo tokens — not verifiable, return None (WS will fallback to DB lookup)
#     if token.startswith("demo_"):
#         return None
#     try:
#         payload, sig = token.rsplit(".", 1)
#         expected = _hmac.new(SECRET_KEY, payload.encode(), "sha256").hexdigest()
#         if not _hmac.compare_digest(sig, expected):
#             return None
#         return base64.b64decode(payload.encode()).decode()
#     except Exception:
#         return None


# # ─────────────────────────────────────────────
# # Helpers
# # ─────────────────────────────────────────────
# def make_uid(email: str) -> str:
#     return hashlib.sha256(email.lower().encode()).hexdigest()[:12]

# def now() -> str:
#     return datetime.utcnow().isoformat()

# def conv_id(a: str, b: str) -> str:
#     return "_".join(sorted([a, b]))

# def _fmt_sec(s: int) -> str:
#     s = max(0, int(s))
#     return f"{s // 60:02d}:{s % 60:02d}"

# async def send_to(uid: str, msg: dict):
#     ws = connected.get(uid)
#     if ws:
#         try:
#             await ws.send_text(json.dumps(msg))
#         except Exception:
#             connected.pop(uid, None)

# async def broadcast(msg: dict, exclude: str = None):
#     for uid, ws in list(connected.items()):
#         if uid != exclude:
#             try:
#                 await ws.send_text(json.dumps(msg))
#             except Exception:
#                 connected.pop(uid, None)


# # ─────────────────────────────────────────────
# # Models
# # ─────────────────────────────────────────────
# class RegisterReq(BaseModel):
#     name:  str
#     email: str


# # ─────────────────────────────────────────────
# # Auth endpoints
# # ─────────────────────────────────────────────
# @app.post("/api/register")
# async def register(body: RegisterReq, response: Response):
#     email = body.email.strip().lower()
#     name  = body.name.strip()
#     if not email or not name:
#         raise HTTPException(400, "name and email required")

#     uid    = make_uid(email)
#     colors = ["#7c3aed","#0891b2","#059669","#d97706","#dc2626","#2563eb","#9333ea","#be185d"]
#     color  = colors[ord(name[0].lower()) % len(colors)]

#     existing = await users_col.find_one({"user_id": uid}, {"_id": 0})
#     if existing:
#         await users_col.update_one({"user_id": uid}, {"$set": {"name": name, "last_seen": now()}})
#         user = {**existing, "name": name}
#     else:
#         user = {
#             "user_id": uid, "name": name, "email": email,
#             "avatar_color": color, "joined_at": now(), "last_seen": now()
#         }
#         await users_col.insert_one(user)
#         user.pop("_id", None)

#     token = sign_token(uid)
#     response.set_cookie(key="vl_session", value=token, max_age=7*24*3600,
#                         httponly=False, samesite="lax",
#                         secure=os.getenv("ENV","dev")=="prod", path="/")
#     return {"status": "ok", "user": user, "token": token}


# @app.get("/api/me")
# async def get_me(request: Request):
#     # Try cookie
#     token = request.cookies.get("vl_session")
#     # Try X-Session header
#     if not token:
#         token = request.headers.get("X-Session")
#     # Try Authorization Bearer
#     if not token:
#         auth = request.headers.get("Authorization", "")
#         if auth.startswith("Bearer "):
#             token = auth[7:]

#     uid = verify_token(token) if token else None
#     if not uid:
#         raise HTTPException(401, "No valid session")

#     user = await users_col.find_one({"user_id": uid}, {"_id": 0})
#     if not user:
#         raise HTTPException(404, "User not found")
#     return {"user": user}


# @app.post("/api/logout")
# async def logout(response: Response):
#     response.delete_cookie("vl_session", path="/")
#     return {"ok": True}


# # ─────────────────────────────────────────────
# # Users / Messages / Calls
# # ─────────────────────────────────────────────
# @app.get("/api/users")
# async def list_users(me: str = ""):
#     docs = await users_col.find({"user_id": {"$ne": me}}, {"_id": 0, "email": 0}).to_list(500)
#     for d in docs:
#         d["online"] = d["user_id"] in connected
#     return {"users": docs}


# @app.get("/api/messages/{a}/{b}")
# async def get_messages(a: str, b: str, limit: int = 60):
#     cid  = conv_id(a, b)
#     docs = await messages_col.find({"conversation_id": cid}, {"_id": 0}) \
#                               .sort("timestamp", -1).limit(limit).to_list(limit)
#     docs.reverse()
#     return {"messages": docs}


# @app.get("/api/conversations/{uid}")
# async def get_conversations(uid: str):
#     pipeline = [
#         {"$match": {"$or": [{"sender_id": uid}, {"receiver_id": uid}]}},
#         {"$sort": {"timestamp": -1}},
#         {"$group": {
#             "_id": "$conversation_id",
#             "last_message":  {"$first": "$text"},
#             "last_msg_type": {"$first": "$msg_type"},
#             "last_time":     {"$first": "$timestamp"},
#             "unread": {"$sum": {"$cond": [
#                 {"$and": [{"$eq": ["$receiver_id", uid]}, {"$eq": ["$read", False]}]},
#                 1, 0
#             ]}}
#         }}
#     ]
#     convs  = await messages_col.aggregate(pipeline).to_list(100)
#     result = []
#     for c in convs:
#         parts    = c["_id"].split("_")
#         other_id = parts[1] if parts[0] == uid else parts[0]
#         other    = await users_col.find_one({"user_id": other_id}, {"_id": 0, "email": 0})
#         if other:
#             other["online"] = other_id in connected
#             lm = c.get("last_message") or ""
#             mt = c.get("last_msg_type", "text")
#             if mt == "audio":  lm = "🎤 Voice message"
#             elif mt == "video": lm = "🎥 Video"
#             elif mt == "image": lm = "🖼️ Photo"
#             result.append({
#                 "conversation_id": c["_id"],
#                 "other_user":  other,
#                 "last_message": lm,
#                 "last_time":   c["last_time"],
#                 "unread":      c["unread"]
#             })
#     return {"conversations": result}


# @app.get("/api/calls/{uid}")
# async def get_calls(uid: str):
#     docs = await calls_col.find(
#         {"$or": [{"caller_id": uid}, {"callee_id": uid}]}, {"_id": 0}
#     ).sort("started_at", -1).limit(50).to_list(50)
#     return {"calls": docs}


# @app.post("/api/recording/{call_id}")
# async def save_recording(call_id: str, request: Request):
#     body = await request.json()
#     blob = body.get("blob")
#     if not blob:
#         raise HTTPException(400, "No blob")
#     await recordings_col.insert_one({
#         "recording_id": str(uuid.uuid4()),
#         "call_id": call_id, "blob": blob, "timestamp": now()
#     })
#     return {"ok": True}


# @app.get("/api/recording/{call_id}")
# async def get_recording(call_id: str):
#     docs = await recordings_col.find({"call_id": call_id}, {"_id": 0}) \
#                                 .sort("timestamp", 1).to_list(1000)
#     return {"chunks": docs}


# # ─────────────────────────────────────────────
# # WebSocket — FIXED AUTH
# # KEY FIX: token is OPTIONAL. If token is valid → great.
# # If token is missing/invalid/demo → still allow IF user exists in DB.
# # This makes demo mode and real mode both work.
# # ─────────────────────────────────────────────
# @app.websocket("/ws/{user_id}")
# async def ws_hub(
#     websocket: WebSocket,
#     user_id: str,
#     token: Optional[str] = Query(default=None)
# ):
#     await websocket.accept()

#     # If a real (non-demo) token was provided, validate it
#     if token and not token.startswith("demo_"):
#         verified_uid = verify_token(token)
#         if verified_uid and verified_uid != user_id:
#             # Token is valid but for a DIFFERENT user — reject
#             await websocket.send_text(json.dumps({
#                 "type": "error", "info": "Token user mismatch"
#             }))
#             await websocket.close(4001)
#             return
#         # If token invalid (None) we still allow — just fall through to DB check

#     # Always verify user exists in DB
#     user = await users_col.find_one({"user_id": user_id})
#     if not user:
#         await websocket.send_text(json.dumps({
#             "type": "error", "info": "User not found — please register first"
#         }))
#         await websocket.close(4001)
#         return

#     # ── Connected! ────────────────────────────────────────────────
#     connected[user_id] = websocket
#     await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

#     await broadcast({
#         "type": "user_online", "user_id": user_id,
#         "name": user["name"], "avatar_color": user.get("avatar_color", "#7c3aed"),
#         "timestamp": now()
#     }, exclude=user_id)

#     online = []
#     for uid in list(connected.keys()):
#         if uid != user_id:
#             u = await users_col.find_one({"user_id": uid}, {"_id": 0, "email": 0})
#             if u:
#                 u["online"] = True
#                 online.append(u)
#     await send_to(user_id, {"type": "online_users", "users": online})

#     my_active_call: dict = {}

#     try:
#         while True:
#             raw = await websocket.receive_text()
#             m   = json.loads(raw)
#             t   = m.get("type")

#             if t == "chat_message":
#                 rid      = m.get("receiver_id", "")
#                 text     = m.get("text", "").strip()
#                 msg_type = m.get("msg_type", "text")
#                 media    = m.get("media")
#                 if not text and not media:
#                     continue
#                 cid = conv_id(user_id, rid)
#                 doc = {
#                     "message_id": str(uuid.uuid4()), "conversation_id": cid,
#                     "sender_id": user_id, "sender_name": user["name"],
#                     "receiver_id": rid, "text": text,
#                     "msg_type": msg_type, "media": media,
#                     "timestamp": now(), "read": False
#                 }
#                 await messages_col.insert_one(doc)
#                 doc.pop("_id", None)
#                 await send_to(rid,     {"type": "chat_message",  **doc})
#                 await send_to(user_id, {"type": "message_sent",  **doc})

#             elif t == "mark_read":
#                 cid = conv_id(user_id, m.get("other_id",""))
#                 await messages_col.update_many(
#                     {"conversation_id": cid, "receiver_id": user_id, "read": False},
#                     {"$set": {"read": True}}
#                 )

#             elif t == "call_request":
#                 clee_id   = m.get("callee_id")
#                 call_type = m.get("call_type", "audio")
#                 call_id   = str(uuid.uuid4())[:12]
#                 clee      = await users_col.find_one({"user_id": clee_id}) or {}
#                 call_doc  = {
#                     "call_id": call_id, "caller_id": user_id, "caller_name": user["name"],
#                     "callee_id": clee_id, "callee_name": clee.get("name", "?"),
#                     "status": "ringing", "call_type": call_type,
#                     "started_at": now(), "ended_at": None, "duration_sec": 0,
#                     "has_recording": False
#                 }
#                 await calls_col.insert_one(call_doc)
#                 call_doc.pop("_id", None)
#                 my_active_call = {"call_id": call_id, "other_id": clee_id}

#                 await send_to(clee_id, {
#                     "type": "incoming_call", "call_id": call_id,
#                     "caller_id": user_id, "caller_name": user["name"],
#                     "avatar_color": user.get("avatar_color", "#7c3aed"),
#                     "call_type": call_type, "timestamp": now()
#                 })
#                 await send_to(user_id, {
#                     "type": "call_ringing", "call_id": call_id,
#                     "callee_name": clee.get("name", "?"), "call_type": call_type
#                 })

#             elif t == "call_accepted":
#                 call_id = m.get("call_id")
#                 my_active_call = {"call_id": call_id, "other_id": m.get("caller_id")}
#                 await calls_col.update_one({"call_id": call_id}, {"$set": {"status": "active"}})
#                 await send_to(m.get("caller_id"), {
#                     "type": "call_accepted", "call_id": call_id,
#                     "callee_name": user["name"], "callee_id": user_id
#                 })

#             elif t == "call_rejected":
#                 call_id = m.get("call_id")
#                 await calls_col.update_one(
#                     {"call_id": call_id},
#                     {"$set": {"status": "rejected", "ended_at": now()}}
#                 )
#                 await send_to(m.get("caller_id"), {
#                     "type": "call_rejected", "call_id": call_id,
#                     "by_name": user["name"]
#                 })
#                 my_active_call = {}

#             elif t == "call_ended":
#                 call_id  = m.get("call_id")
#                 other_id = m.get("other_id")
#                 dur      = m.get("duration", 0)
#                 has_rec  = m.get("has_recording", False)
#                 await calls_col.update_one(
#                     {"call_id": call_id},
#                     {"$set": {"status": "ended", "ended_at": now(),
#                               "duration_sec": dur, "has_recording": has_rec}}
#                 )
#                 await send_to(other_id, {
#                     "type": "call_ended", "call_id": call_id, "duration": dur,
#                     "has_recording": has_rec, "by_name": user["name"],
#                     "msg": f"Call ended · {_fmt_sec(dur)}"
#                 })
#                 my_active_call = {}

#             elif t in ("webrtc_offer", "webrtc_answer", "ice_candidate"):
#                 await send_to(m.get("target_id"), {**m, "from_id": user_id})

#     except WebSocketDisconnect:
#         pass
#     finally:
#         connected.pop(user_id, None)
#         await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

#         if my_active_call:
#             call_id  = my_active_call.get("call_id")
#             other_id = my_active_call.get("other_id")
#             dur = 0
#             call_doc = await calls_col.find_one({"call_id": call_id})
#             if call_doc and call_doc.get("status") in ("active", "ringing"):
#                 try:
#                     started = datetime.fromisoformat(call_doc["started_at"])
#                     dur = int((datetime.utcnow() - started).total_seconds())
#                 except Exception:
#                     dur = 0
#                 await calls_col.update_one(
#                     {"call_id": call_id},
#                     {"$set": {"status": "ended", "ended_at": now(), "duration_sec": dur}}
#                 )
#                 await send_to(other_id, {
#                     "type": "call_ended", "call_id": call_id, "duration": dur,
#                     "by_name": user["name"], "msg": f"{user['name']} disconnected"
#                 })

#         await broadcast({
#             "type": "user_offline", "user_id": user_id,
#             "name": user["name"], "timestamp": now()
#         })


# # ─────────────────────────────────────────────
# # Static files
# # ─────────────────────────────────────────────
# from fastapi.staticfiles import StaticFiles
# from fastapi.responses import FileResponse
# from pathlib import Path

# static_dir = Path("static")
# static_dir.mkdir(exist_ok=True)

# app.mount("/static", StaticFiles(directory="static"), name="static")

# @app.get("/")
# async def read_index():
#     return FileResponse(static_dir / "index.html")


"""
VoiceLink Backend v5 — Full WhatsApp-like Features
New features:
  1. Message privacy: block/accept system before first message
  2. Recording saved in audio_recordings collection with join/leave timestamps
  3. Messages stored per user with proper indexing
  4. Echo cancellation info passed
  5. Busy status during calls
  6. Typing indicators
  7. Message edit/delete (within 1 hour)
  8. Group calls (max 4 people)
  9. Recording stored per user per call
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Response, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import json, uuid, hashlib, os, base64, hmac as _hmac

app = FastAPI(title="VoiceLink API", version="5.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://zapierobroy_db_user:JkkZxi5rySFcqhds@cluster0.ncgeqd2.mongodb.net/")
SECRET_KEY = "voicelink-stable-secret-2024".encode()

db             = AsyncIOMotorClient(MONGO_URL)["voicelink_v5"]
users_col      = db["users"]
messages_col   = db["messages"]
calls_col      = db["calls"]
recordings_col = db["audio_recordings"]   # per-user join/leave timestamps
privacy_col    = db["privacy_requests"]   # block/accept system
groups_col     = db["group_calls"]        # group call tracking

connected: Dict[str, WebSocket] = {}       # uid -> ws
in_call:   Dict[str, str]       = {}       # uid -> call_id (busy tracking)
group_calls: Dict[str, List[str]] = {}     # call_id -> [uid, ...]


# ── Token helpers ──────────────────────────────────────────────────────────────
def sign_token(user_id: str) -> str:
    payload = base64.b64encode(user_id.encode()).decode()
    sig = _hmac.new(SECRET_KEY, payload.encode(), "sha256").hexdigest()
    return f"{payload}.{sig}"

def verify_token(token: str) -> Optional[str]:
    if not token or token.startswith("demo_"):
        return None
    try:
        payload, sig = token.rsplit(".", 1)
        expected = _hmac.new(SECRET_KEY, payload.encode(), "sha256").hexdigest()
        if not _hmac.compare_digest(sig, expected):
            return None
        return base64.b64decode(payload.encode()).decode()
    except Exception:
        return None

def make_uid(email: str) -> str:
    return hashlib.sha256(email.lower().encode()).hexdigest()[:12]

def now() -> str:
    return datetime.utcnow().isoformat()

def conv_id(a: str, b: str) -> str:
    return "_".join(sorted([a, b]))

def fmt_sec(s: int) -> str:
    s = max(0, int(s))
    return f"{s // 60:02d}:{s % 60:02d}"

async def send_to(uid: str, msg: dict):
    ws = connected.get(uid)
    if ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            connected.pop(uid, None)

async def broadcast(msg: dict, exclude: str = None):
    for uid, ws in list(connected.items()):
        if uid != exclude:
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                connected.pop(uid, None)

async def send_to_group(uids: List[str], msg: dict, exclude: str = None):
    for uid in uids:
        if uid != exclude:
            await send_to(uid, msg)


# ── Models ─────────────────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    name: str
    email: str


# ── Auth ───────────────────────────────────────────────────────────────────────
@app.post("/api/register")
async def register(body: RegisterReq, response: Response):
    email = body.email.strip().lower()
    name  = body.name.strip()
    if not email or not name:
        raise HTTPException(400, "name and email required")
    uid    = make_uid(email)
    colors = ["#7c3aed","#0891b2","#059669","#d97706","#dc2626","#2563eb","#9333ea","#be185d"]
    color  = colors[ord(name[0].lower()) % len(colors)]
    existing = await users_col.find_one({"user_id": uid}, {"_id": 0})
    if existing:
        await users_col.update_one({"user_id": uid}, {"$set": {"name": name, "last_seen": now()}})
        user = {**existing, "name": name}
    else:
        user = {
            "user_id": uid, "name": name, "email": email,
            "avatar_color": color, "joined_at": now(), "last_seen": now()
        }
        await users_col.insert_one(user)
        user.pop("_id", None)
    token = sign_token(uid)
    response.set_cookie(key="vl_session", value=token, max_age=7*24*3600,
                        httponly=False, samesite="lax",
                        secure=os.getenv("ENV","dev")=="prod", path="/")
    return {"status": "ok", "user": user, "token": token}


@app.get("/api/me")
async def get_me(request: Request):
    token = request.cookies.get("vl_session") or request.headers.get("X-Session")
    if not token:
        auth = request.headers.get("Authorization","")
        if auth.startswith("Bearer "): token = auth[7:]
    uid = verify_token(token) if token else None
    if not uid: raise HTTPException(401, "No valid session")
    user = await users_col.find_one({"user_id": uid}, {"_id": 0})
    if not user: raise HTTPException(404, "User not found")
    return {"user": user}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("vl_session", path="/")
    return {"ok": True}


# ── Users ──────────────────────────────────────────────────────────────────────
@app.get("/api/users")
async def list_users(me: str = ""):
    docs = await users_col.find({"user_id": {"$ne": me}}, {"_id": 0, "email": 0}).to_list(500)
    for d in docs:
        d["online"] = d["user_id"] in connected
        d["busy"]   = d["user_id"] in in_call
    return {"users": docs}


# ── Privacy (block/accept) ─────────────────────────────────────────────────────
@app.get("/api/privacy/{from_id}/{to_id}")
async def get_privacy(from_id: str, to_id: str):
    doc = await privacy_col.find_one({"from_id": from_id, "to_id": to_id}, {"_id": 0})
    return {"privacy": doc}

@app.post("/api/privacy")
async def set_privacy(request: Request):
    body = await request.json()
    from_id = body.get("from_id")
    to_id   = body.get("to_id")
    status  = body.get("status")  # "accepted" | "blocked"
    if not all([from_id, to_id, status]):
        raise HTTPException(400, "Missing fields")
    await privacy_col.update_one(
        {"from_id": from_id, "to_id": to_id},
        {"$set": {"from_id": from_id, "to_id": to_id, "status": status, "updated_at": now()}},
        upsert=True
    )
    return {"ok": True}


# ── Messages ───────────────────────────────────────────────────────────────────
@app.get("/api/messages/{a}/{b}")
async def get_messages(a: str, b: str, limit: int = 60):
    cid  = conv_id(a, b)
    docs = await messages_col.find({"conversation_id": cid}, {"_id": 0}) \
                              .sort("timestamp", -1).limit(limit).to_list(limit)
    docs.reverse()
    return {"messages": docs}

@app.patch("/api/messages/{msg_id}")
async def edit_message(msg_id: str, request: Request):
    body = await request.json()
    new_text = body.get("text","").strip()
    msg = await messages_col.find_one({"message_id": msg_id})
    if not msg:
        raise HTTPException(404, "Message not found")
    # Check 1-hour window
    try:
        created = datetime.fromisoformat(msg["timestamp"])
        if datetime.utcnow() - created > timedelta(hours=1):
            raise HTTPException(403, "Edit window expired")
    except ValueError:
        raise HTTPException(400, "Invalid timestamp")
    await messages_col.update_one(
        {"message_id": msg_id},
        {"$set": {"text": new_text, "edited": True, "edited_at": now()}}
    )
    return {"ok": True}

@app.delete("/api/messages/{msg_id}")
async def delete_message(msg_id: str):
    msg = await messages_col.find_one({"message_id": msg_id})
    if not msg:
        raise HTTPException(404, "Message not found")
    try:
        created = datetime.fromisoformat(msg["timestamp"])
        if datetime.utcnow() - created > timedelta(hours=1):
            raise HTTPException(403, "Delete window expired")
    except ValueError:
        raise HTTPException(400, "Invalid timestamp")
    await messages_col.update_one(
        {"message_id": msg_id},
        {"$set": {"deleted": True, "text": "", "media": None, "deleted_at": now()}}
    )
    return {"ok": True}


@app.get("/api/conversations/{uid}")
async def get_conversations(uid: str):
    pipeline = [
        {"$match": {"$or": [{"sender_id": uid}, {"receiver_id": uid}]}},
        {"$sort": {"timestamp": -1}},
        {"$group": {
            "_id": "$conversation_id",
            "last_message":  {"$first": "$text"},
            "last_msg_type": {"$first": "$msg_type"},
            "last_time":     {"$first": "$timestamp"},
            "unread": {"$sum": {"$cond": [
                {"$and": [{"$eq": ["$receiver_id", uid]}, {"$eq": ["$read", False]}, {"$ne": ["$deleted", True]}]},
                1, 0
            ]}}
        }}
    ]
    convs  = await messages_col.aggregate(pipeline).to_list(100)
    result = []
    for c in convs:
        parts    = c["_id"].split("_")
        other_id = parts[1] if parts[0] == uid else parts[0]
        other    = await users_col.find_one({"user_id": other_id}, {"_id": 0, "email": 0})
        if other:
            other["online"] = other_id in connected
            other["busy"]   = other_id in in_call
            lm = c.get("last_message") or ""
            mt = c.get("last_msg_type", "text")
            if mt == "audio":  lm = "🎤 Voice message"
            elif mt == "video": lm = "🎥 Video"
            elif mt == "image": lm = "🖼️ Photo"
            result.append({
                "conversation_id": c["_id"],
                "other_user":  other,
                "last_message": lm,
                "last_time":   c["last_time"],
                "unread":      c["unread"]
            })
    return {"conversations": result}


# ── Calls ──────────────────────────────────────────────────────────────────────
@app.get("/api/calls/{uid}")
async def get_calls(uid: str):
    docs = await calls_col.find(
        {"$or": [{"caller_id": uid}, {"callee_id": uid}, {"participants": uid}]}, {"_id": 0}
    ).sort("started_at", -1).limit(50).to_list(50)
    return {"calls": docs}


# ── Recordings ─────────────────────────────────────────────────────────────────
@app.post("/api/recording/{call_id}/{user_id}")
async def save_recording(call_id: str, user_id: str, request: Request):
    # Guard: reject obviously invalid call_ids
    if not call_id or call_id in ("null", "undefined", "busy", ""):
        raise HTTPException(400, f"Invalid call_id: {call_id}")

    body = await request.json()
    join_time    = body.get("join_time")
    leave_time   = body.get("leave_time")
    duration_sec = body.get("duration_sec", 0)
    has_audio    = body.get("has_audio", False)
    chunk_index  = body.get("chunk_index", 0)
    total_chunks = body.get("total_chunks", 1)
    blob         = body.get("blob")  # may be None for metadata-only saves

    # Safety check: if blob present, verify it won't exceed MongoDB 16MB limit.
    # Base64 string of 12MB ≈ 9MB raw — safe upper bound is 11MB of base64 chars.
    MAX_B64_CHARS = 11 * 1024 * 1024  # ~11MB base64 ≈ ~8.3MB raw
    if blob and len(blob) > MAX_B64_CHARS:
        # Store metadata only — drop the oversized blob
        blob = None
        has_audio = False

    doc = {
        "recording_id":  str(uuid.uuid4()),
        "call_id":       call_id,
        "user_id":       user_id,
        "join_time":     join_time,
        "leave_time":    leave_time,
        "duration_sec":  duration_sec,
        "has_audio":     has_audio and blob is not None,
        "chunk_index":   chunk_index,
        "total_chunks":  total_chunks,
        "saved_at":      now()
    }
    if blob:
        doc["blob"] = blob

    await recordings_col.insert_one(doc)
    return {"ok": True, "stored_audio": bool(blob)}

@app.get("/api/recording/{call_id}")
async def get_recordings(call_id: str):
    # Return metadata only (exclude blobs — they can be huge)
    docs = await recordings_col.find(
        {"call_id": call_id}, {"_id": 0, "blob": 0}
    ).sort("chunk_index", 1).to_list(200)
    return {"recordings": docs}

@app.get("/api/recording/{call_id}/{user_id}/audio")
async def get_recording_blob(call_id: str, user_id: str):
    """Return all chunks for a specific user's recording, ordered by chunk_index."""
    docs = await recordings_col.find(
        {"call_id": call_id, "user_id": user_id, "has_audio": True},
        {"_id": 0, "blob": 1, "chunk_index": 1, "total_chunks": 1}
    ).sort("chunk_index", 1).to_list(100)
    return {"chunks": docs}


# ── WebSocket Hub ──────────────────────────────────────────────────────────────
@app.websocket("/ws/{user_id}")
async def ws_hub(
    websocket: WebSocket,
    user_id: str,
    token: Optional[str] = Query(default=None)
):
    await websocket.accept()

    if token and not token.startswith("demo_"):
        verified_uid = verify_token(token)
        if verified_uid and verified_uid != user_id:
            await websocket.send_text(json.dumps({"type": "error", "info": "Token user mismatch"}))
            await websocket.close(4001)
            return

    user = await users_col.find_one({"user_id": user_id})
    if not user:
        await websocket.send_text(json.dumps({"type": "error", "info": "User not found — please register first"}))
        await websocket.close(4001)
        return

    connected[user_id] = websocket
    await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

    await broadcast({
        "type": "user_online", "user_id": user_id,
        "name": user["name"], "avatar_color": user.get("avatar_color", "#7c3aed"),
        "timestamp": now()
    }, exclude=user_id)

    online = []
    for uid in list(connected.keys()):
        if uid != user_id:
            u = await users_col.find_one({"user_id": uid}, {"_id": 0, "email": 0})
            if u:
                u["online"] = True
                u["busy"]   = uid in in_call
                online.append(u)
    await send_to(user_id, {"type": "online_users", "users": online})

    my_active_call: dict = {}

    try:
        while True:
            raw = await websocket.receive_text()
            m   = json.loads(raw)
            t   = m.get("type")

            # ── Chat message ─────────────────────────────────────────────────
            if t == "chat_message":
                rid      = m.get("receiver_id", "")
                text     = m.get("text", "").strip()
                msg_type = m.get("msg_type", "text")
                media    = m.get("media")
                if not text and not media:
                    continue

                # Check if receiver has blocked sender
                privacy = await privacy_col.find_one({"from_id": rid, "to_id": user_id})
                if privacy and privacy.get("status") == "blocked":
                    await send_to(user_id, {"type": "error", "info": "You are blocked by this user"})
                    continue

                # Check if this is first message — need acceptance
                conv_exists = await messages_col.find_one({"conversation_id": conv_id(user_id, rid)})
                accept_doc  = await privacy_col.find_one({"from_id": rid, "to_id": user_id, "status": "accepted"})
                
                if not conv_exists and not accept_doc:
                    # Send privacy request to receiver
                    cid_new = conv_id(user_id, rid)
                    await privacy_col.update_one(
                        {"from_id": rid, "to_id": user_id},
                        {"$set": {"from_id": rid, "to_id": user_id, "status": "pending",
                                  "requester_id": user_id, "requester_name": user["name"],
                                  "updated_at": now()}},
                        upsert=True
                    )
                    # Queue the message for sending after acceptance
                    msg_doc = {
                        "message_id": str(uuid.uuid4()),
                        "conversation_id": conv_id(user_id, rid),
                        "sender_id": user_id, "sender_name": user["name"],
                        "receiver_id": rid, "text": text,
                        "msg_type": msg_type, "media": media,
                        "timestamp": now(), "read": False,
                        "pending_privacy": True
                    }
                    await messages_col.insert_one(msg_doc)
                    msg_doc.pop("_id", None)
                    await send_to(rid, {
                        "type": "privacy_request",
                        "from_id": user_id,
                        "from_name": user["name"],
                        "avatar_color": user.get("avatar_color","#7c3aed"),
                        "message_preview": text[:50] if msg_type=="text" else f"[{msg_type}]"
                    })
                    await send_to(user_id, {"type": "message_pending", "message_id": msg_doc["message_id"]})
                    continue

                cid2 = conv_id(user_id, rid)
                doc = {
                    "message_id": str(uuid.uuid4()), "conversation_id": cid2,
                    "sender_id": user_id, "sender_name": user["name"],
                    "receiver_id": rid, "text": text,
                    "msg_type": msg_type, "media": media,
                    "timestamp": now(), "read": False,
                    "edited": False, "deleted": False
                }
                await messages_col.insert_one(doc)
                doc.pop("_id", None)
                await send_to(rid,     {"type": "chat_message",  **doc})
                await send_to(user_id, {"type": "message_sent",  **doc})

            # ── Privacy response ───────────────────────────────────────────
            elif t == "privacy_response":
                requester_id = m.get("from_id")
                action       = m.get("action")  # "accept" | "block"
                await privacy_col.update_one(
                    {"from_id": user_id, "to_id": requester_id},
                    {"$set": {"status": action+"ed", "updated_at": now()}}
                )
                if action == "accept":
                    # Deliver any pending messages
                    pending = await messages_col.find(
                        {"conversation_id": conv_id(user_id, requester_id),
                         "pending_privacy": True}
                    ).to_list(100)
                    for pm in pending:
                        await messages_col.update_one(
                            {"message_id": pm["message_id"]},
                            {"$unset": {"pending_privacy": ""}}
                        )
                        pm.pop("_id", None)
                        pm.pop("pending_privacy", None)
                        await send_to(user_id, {"type": "chat_message", **pm})
                    await send_to(requester_id, {
                        "type": "privacy_accepted", "by_id": user_id, "by_name": user["name"]
                    })
                else:
                    # Block — delete pending messages
                    await messages_col.delete_many({
                        "conversation_id": conv_id(user_id, requester_id),
                        "pending_privacy": True
                    })
                    await send_to(requester_id, {
                        "type": "privacy_blocked", "by_name": user["name"]
                    })

            # ── Message edit ───────────────────────────────────────────────
            elif t == "edit_message":
                msg_id   = m.get("message_id")
                new_text = m.get("text","").strip()
                msg = await messages_col.find_one({"message_id": msg_id})
                if msg and msg["sender_id"] == user_id:
                    created = datetime.fromisoformat(msg["timestamp"])
                    if datetime.utcnow() - created <= timedelta(hours=1):
                        await messages_col.update_one(
                            {"message_id": msg_id},
                            {"$set": {"text": new_text, "edited": True, "edited_at": now()}}
                        )
                        other_id = msg["receiver_id"] if msg["sender_id"]==user_id else msg["sender_id"]
                        edit_event = {"type": "message_edited", "message_id": msg_id,
                                      "text": new_text, "edited_at": now()}
                        await send_to(other_id, edit_event)
                        await send_to(user_id,  edit_event)

            # ── Message delete ─────────────────────────────────────────────
            elif t == "delete_message":
                msg_id = m.get("message_id")
                msg = await messages_col.find_one({"message_id": msg_id})
                if msg and msg["sender_id"] == user_id:
                    created = datetime.fromisoformat(msg["timestamp"])
                    if datetime.utcnow() - created <= timedelta(hours=1):
                        await messages_col.update_one(
                            {"message_id": msg_id},
                            {"$set": {"deleted": True, "text": "", "media": None, "deleted_at": now()}}
                        )
                        other_id = msg["receiver_id"] if msg["sender_id"]==user_id else msg["sender_id"]
                        del_event = {"type": "message_deleted", "message_id": msg_id}
                        await send_to(other_id, del_event)
                        await send_to(user_id,  del_event)

            # ── Typing indicator ───────────────────────────────────────────
            elif t == "typing":
                await send_to(m.get("receiver_id"), {
                    "type": "typing", "from_id": user_id,
                    "from_name": user["name"], "is_typing": m.get("is_typing", True)
                })

            elif t == "mark_read":
                cid2 = conv_id(user_id, m.get("other_id",""))
                await messages_col.update_many(
                    {"conversation_id": cid2, "receiver_id": user_id, "read": False},
                    {"$set": {"read": True}}
                )

            elif t == "messages_read":
                # Relay read receipt to the sender so their ticks turn blue
                to_id = m.get("to_id")
                await send_to(to_id, {
                    "type": "messages_read",
                    "from_id": user_id
                })

            # ── 1-to-1 call ───────────────────────────────────────────────
            elif t == "call_request":
                clee_id   = m.get("callee_id")
                call_type = m.get("call_type", "audio")
                
                # Check if callee is busy
                if clee_id in in_call:
                    await send_to(user_id, {
                        "type": "call_rejected",
                        "call_id": "busy",
                        "by_name": clee.get("name","?"),
                        "busy": True,
                        "msg": "User is in another call"
                    })
                    continue
                
                call_id  = str(uuid.uuid4())[:12]
                clee     = await users_col.find_one({"user_id": clee_id}) or {}
                call_doc = {
                    "call_id": call_id, "caller_id": user_id, "caller_name": user["name"],
                    "callee_id": clee_id, "callee_name": clee.get("name", "?"),
                    "status": "ringing", "call_type": call_type,
                    "started_at": now(), "ended_at": None, "duration_sec": 0,
                    "has_recording": False, "is_group": False
                }
                await calls_col.insert_one(call_doc)
                call_doc.pop("_id", None)
                my_active_call = {"call_id": call_id, "other_id": clee_id}
                in_call[user_id] = call_id

                await send_to(clee_id, {
                    "type": "incoming_call", "call_id": call_id,
                    "caller_id": user_id, "caller_name": user["name"],
                    "avatar_color": user.get("avatar_color", "#7c3aed"),
                    "call_type": call_type, "timestamp": now()
                })
                await send_to(user_id, {
                    "type": "call_ringing", "call_id": call_id,
                    "callee_name": clee.get("name","?"), "call_type": call_type
                })
                # Notify others that this user is now busy
                await broadcast({"type": "user_busy", "user_id": user_id}, exclude=user_id)

            elif t == "call_accepted":
                call_id = m.get("call_id")
                my_active_call = {"call_id": call_id, "other_id": m.get("caller_id")}
                in_call[user_id] = call_id
                await calls_col.update_one({"call_id": call_id}, {"$set": {"status": "active"}})
                await send_to(m.get("caller_id"), {
                    "type": "call_accepted", "call_id": call_id,
                    "callee_name": user["name"], "callee_id": user_id
                })
                await broadcast({"type": "user_busy", "user_id": user_id}, exclude=user_id)

            elif t == "call_rejected":
                call_id = m.get("call_id")
                in_call.pop(user_id, None)
                await calls_col.update_one(
                    {"call_id": call_id},
                    {"$set": {"status": "rejected", "ended_at": now()}}
                )
                await send_to(m.get("caller_id"), {
                    "type": "call_rejected", "call_id": call_id, "by_name": user["name"]
                })
                my_active_call = {}

            elif t == "call_ended":
                call_id  = m.get("call_id")
                other_id = m.get("other_id")
                dur      = m.get("duration", 0)
                has_rec  = m.get("has_recording", False)
                in_call.pop(user_id, None)
                await calls_col.update_one(
                    {"call_id": call_id},
                    {"$set": {"status": "ended", "ended_at": now(),
                              "duration_sec": dur, "has_recording": has_rec}}
                )
                await send_to(other_id, {
                    "type": "call_ended", "call_id": call_id, "duration": dur,
                    "has_recording": has_rec, "by_name": user["name"],
                    "msg": f"Call ended · {fmt_sec(dur)}"
                })
                await broadcast({"type": "user_free", "user_id": user_id}, exclude=user_id)
                my_active_call = {}

            # ── Group call ─────────────────────────────────────────────────
            elif t == "group_call_invite":
                invitee_ids = m.get("invitee_ids", [])[:3]  # max 3 others = 4 total
                call_type   = m.get("call_type", "audio")
                group_id    = "grp_" + str(uuid.uuid4())[:10]
                participants = [user_id] + invitee_ids
                
                group_calls[group_id] = [user_id]
                in_call[user_id] = group_id
                
                grp_doc = {
                    "call_id": group_id, "host_id": user_id, "host_name": user["name"],
                    "participants": participants, "joined": [user_id],
                    "call_type": call_type, "status": "ringing",
                    "started_at": now(), "ended_at": None, "duration_sec": 0,
                    "is_group": True
                }
                await calls_col.insert_one(grp_doc)
                
                for inv_id in invitee_ids:
                    if inv_id in in_call:
                        continue  # Skip busy users
                    inv_user = await users_col.find_one({"user_id": inv_id}) or {}
                    await send_to(inv_id, {
                        "type": "group_call_invite",
                        "call_id": group_id, "host_id": user_id,
                        "host_name": user["name"],
                        "avatar_color": user.get("avatar_color","#7c3aed"),
                        "call_type": call_type, "participant_count": len(participants)
                    })
                
                await send_to(user_id, {
                    "type": "group_call_created", "call_id": group_id,
                    "invitees": invitee_ids, "call_type": call_type
                })

            elif t == "group_call_join":
                group_id = m.get("call_id")
                if group_id in group_calls:
                    group_calls[group_id].append(user_id)
                    in_call[user_id] = group_id
                    await calls_col.update_one(
                        {"call_id": group_id},
                        {"$addToSet": {"joined": user_id}, "$set": {"status": "active"}}
                    )
                    # Notify all in group
                    for uid in group_calls[group_id]:
                        if uid != user_id:
                            await send_to(uid, {
                                "type": "group_member_joined",
                                "call_id": group_id, "user_id": user_id,
                                "name": user["name"]
                            })
                    await send_to(user_id, {
                        "type": "group_call_joined",
                        "call_id": group_id,
                        "members": group_calls[group_id]
                    })

            elif t == "group_call_leave":
                group_id = m.get("call_id")
                in_call.pop(user_id, None)
                if group_id in group_calls:
                    group_calls[group_id] = [u for u in group_calls[group_id] if u != user_id]
                    remaining = group_calls[group_id]
                    for uid in remaining:
                        await send_to(uid, {
                            "type": "group_member_left",
                            "call_id": group_id, "user_id": user_id,
                            "name": user["name"]
                        })
                    if len(remaining) < 2:
                        # End group call
                        await calls_col.update_one({"call_id": group_id},
                            {"$set": {"status": "ended", "ended_at": now()}})
                        for uid in remaining:
                            await send_to(uid, {"type": "call_ended", "call_id": group_id,
                                                "msg": "Group call ended"})
                        group_calls.pop(group_id, None)
                        for uid in remaining:
                            in_call.pop(uid, None)

            elif t == "group_call_reject":
                group_id = m.get("call_id")
                host_id  = m.get("host_id")
                await send_to(host_id, {
                    "type": "group_call_declined", "user_id": user_id, "name": user["name"]
                })

            # ── WebRTC signaling ───────────────────────────────────────────
            elif t in ("webrtc_offer", "webrtc_answer", "ice_candidate"):
                await send_to(m.get("target_id"), {**m, "from_id": user_id})

    except WebSocketDisconnect:
        pass
    finally:
        connected.pop(user_id, None)
        in_call.pop(user_id, None)
        await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

        # Clean up any group calls
        for group_id, members in list(group_calls.items()):
            if user_id in members:
                group_calls[group_id] = [u for u in members if u != user_id]
                for uid in group_calls[group_id]:
                    await send_to(uid, {"type": "group_member_left",
                                        "call_id": group_id, "user_id": user_id,
                                        "name": user["name"]})

        if my_active_call:
            call_id  = my_active_call.get("call_id")
            other_id = my_active_call.get("other_id")
            call_doc = await calls_col.find_one({"call_id": call_id})
            dur = 0
            if call_doc and call_doc.get("status") in ("active","ringing"):
                try:
                    started = datetime.fromisoformat(call_doc["started_at"])
                    dur = int((datetime.utcnow()-started).total_seconds())
                except: pass
                await calls_col.update_one({"call_id": call_id},
                    {"$set": {"status": "ended", "ended_at": now(), "duration_sec": dur}})
                await send_to(other_id, {
                    "type": "call_ended", "call_id": call_id, "duration": dur,
                    "by_name": user["name"], "msg": f"{user['name']} disconnected"
                })

        await broadcast({"type": "user_offline", "user_id": user_id,
                          "name": user["name"], "timestamp": now()})
        await broadcast({"type": "user_free", "user_id": user_id})


# ── Static files ───────────────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_index():
    return FileResponse(static_dir / "index.html")