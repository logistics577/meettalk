"""
VoiceLink Backend v5 — Socket.IO Edition
Replaced FastAPI WebSocket with python-socketio for Render compatibility.
All logic identical to v5 WebSocket version.

Install deps:
  pip install fastapi uvicorn python-socketio motor pydantic
"""

from fastapi import FastAPI, HTTPException, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import json, uuid, hashlib, os, base64, hmac as _hmac
import socketio
import os
from dotenv import load_dotenv
import os

load_dotenv()





# ── Socket.IO setup ────────────────────────────────────────────────────────────
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

fastapi_app = FastAPI(title="VoiceLink API", version="5.0.0")

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Socket.IO on top of FastAPI
app = socketio.ASGIApp(sio, other_asgi_app=fastapi_app)

# ── DB ─────────────────────────────────────────────────────────────────────────
MONGO_URL  = os.getenv("MONGO_URL")
print(MONGO_URL)
SECRET_KEY = "voicelink-stable-secret-2024".encode()

db             = AsyncIOMotorClient(MONGO_URL)["voicelink_v5"]
users_col      = db["users"]
messages_col   = db["messages"]
calls_col      = db["calls"]
recordings_col = db["audio_recordings"]
privacy_col    = db["privacy_requests"]
groups_col     = db["group_calls"]

# ── In-memory state ────────────────────────────────────────────────────────────
# uid  -> socket sid
connected: Dict[str, str]       = {}
# sid  -> uid  (reverse lookup)
sid_to_uid: Dict[str, str]      = {}
# uid  -> call_id
in_call:   Dict[str, str]       = {}
# call_id -> [uid, ...]
group_calls: Dict[str, List[str]] = {}
# sid -> my_active_call dict
active_calls: Dict[str, dict]   = {}


# ── Helpers ────────────────────────────────────────────────────────────────────
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

async def send_to(uid: str, event: str, data: dict):
    """Emit an event to a specific user by uid."""
    sid = connected.get(uid)
    if sid:
        await sio.emit(event, data, to=sid)

async def broadcast_event(event: str, data: dict, exclude: str = None):
    """Emit an event to all connected users except one uid."""
    for uid, sid in list(connected.items()):
        if uid != exclude:
            await sio.emit(event, data, to=sid)


# ── REST Auth ──────────────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    name: str
    email: str

@fastapi_app.post("/api/register")
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


@fastapi_app.get("/api/me")
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


@fastapi_app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie("vl_session", path="/")
    return {"ok": True}


# ── REST Users ─────────────────────────────────────────────────────────────────
@fastapi_app.get("/api/users")
async def list_users(me: str = ""):
    docs = await users_col.find({"user_id": {"$ne": me}}, {"_id": 0, "email": 0}).to_list(500)
    for d in docs:
        d["online"] = d["user_id"] in connected
        d["busy"]   = d["user_id"] in in_call
    return {"users": docs}


# ── REST Privacy ───────────────────────────────────────────────────────────────
@fastapi_app.get("/api/privacy/{from_id}/{to_id}")
async def get_privacy(from_id: str, to_id: str):
    doc = await privacy_col.find_one({"from_id": from_id, "to_id": to_id}, {"_id": 0})
    return {"privacy": doc}

@fastapi_app.post("/api/privacy")
async def set_privacy(request: Request):
    body = await request.json()
    from_id = body.get("from_id")
    to_id   = body.get("to_id")
    status  = body.get("status")
    if not all([from_id, to_id, status]):
        raise HTTPException(400, "Missing fields")
    await privacy_col.update_one(
        {"from_id": from_id, "to_id": to_id},
        {"$set": {"from_id": from_id, "to_id": to_id, "status": status, "updated_at": now()}},
        upsert=True
    )
    return {"ok": True}


# ── REST Messages ──────────────────────────────────────────────────────────────
@fastapi_app.get("/api/messages/{a}/{b}")
async def get_messages(a: str, b: str, limit: int = 60):
    cid  = conv_id(a, b)
    docs = await messages_col.find({"conversation_id": cid}, {"_id": 0}) \
                              .sort("timestamp", -1).limit(limit).to_list(limit)
    docs.reverse()
    return {"messages": docs}

@fastapi_app.patch("/api/messages/{msg_id}")
async def edit_message(msg_id: str, request: Request):
    body = await request.json()
    new_text = body.get("text","").strip()
    msg = await messages_col.find_one({"message_id": msg_id})
    if not msg:
        raise HTTPException(404, "Message not found")
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

@fastapi_app.delete("/api/messages/{msg_id}")
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

@fastapi_app.get("/api/conversations/{uid}")
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


# ── REST Calls ─────────────────────────────────────────────────────────────────
@fastapi_app.get("/api/calls/{uid}")
async def get_calls(uid: str):
    docs = await calls_col.find(
        {"$or": [{"caller_id": uid}, {"callee_id": uid}, {"participants": uid}]}, {"_id": 0}
    ).sort("started_at", -1).limit(50).to_list(50)
    return {"calls": docs}


# ── REST Recordings ────────────────────────────────────────────────────────────
@fastapi_app.post("/api/recording/{call_id}/{user_id}")
async def save_recording(call_id: str, user_id: str, request: Request):
    if not call_id or call_id in ("null", "undefined", "busy", ""):
        raise HTTPException(400, f"Invalid call_id: {call_id}")

    body = await request.json()
    join_time    = body.get("join_time")
    leave_time   = body.get("leave_time")
    duration_sec = body.get("duration_sec", 0)
    has_audio    = body.get("has_audio", False)
    chunk_index  = body.get("chunk_index", 0)
    total_chunks = body.get("total_chunks", 1)
    blob         = body.get("blob")

    MAX_B64_CHARS = 11 * 1024 * 1024
    if blob and len(blob) > MAX_B64_CHARS:
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

@fastapi_app.get("/api/recording/{call_id}")
async def get_recordings(call_id: str):
    docs = await recordings_col.find(
        {"call_id": call_id}, {"_id": 0, "blob": 0}
    ).sort("chunk_index", 1).to_list(200)
    return {"recordings": docs}

@fastapi_app.get("/api/recording/{call_id}/{user_id}/audio")
async def get_recording_blob(call_id: str, user_id: str):
    docs = await recordings_col.find(
        {"call_id": call_id, "user_id": user_id, "has_audio": True},
        {"_id": 0, "blob": 1, "chunk_index": 1, "total_chunks": 1}
    ).sort("chunk_index", 1).to_list(100)
    return {"chunks": docs}


# ── Socket.IO Events ───────────────────────────────────────────────────────────

@sio.event
async def connect(sid, environ, auth):
    """
    Client must pass auth = { user_id, token }
    e.g. io({ auth: { user_id: "abc", token: "xyz" } })
    """
    user_id = (auth or {}).get("user_id")
    token   = (auth or {}).get("token")

    if not user_id:
        return False  # reject

    if token and not token.startswith("demo_"):
        verified = verify_token(token)
        if verified and verified != user_id:
            return False  # token mismatch

    user = await users_col.find_one({"user_id": user_id})
    if not user:
        return False  # unknown user

    # Store mappings
    connected[user_id] = sid
    sid_to_uid[sid]    = user_id
    active_calls[sid]  = {}

    await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

    # Broadcast presence
    await broadcast_event("user_online", {
        "user_id": user_id, "name": user["name"],
        "avatar_color": user.get("avatar_color", "#7c3aed"),
        "timestamp": now()
    }, exclude=user_id)

    # Send current online list to newcomer
    online = []
    for uid in list(connected.keys()):
        if uid != user_id:
            u = await users_col.find_one({"user_id": uid}, {"_id": 0, "email": 0})
            if u:
                u["online"] = True
                u["busy"]   = uid in in_call
                online.append(u)
    await sio.emit("online_users", {"users": online}, to=sid)


@sio.event
async def disconnect(sid):
    user_id = sid_to_uid.pop(sid, None)
    if not user_id:
        return

    connected.pop(user_id, None)
    in_call.pop(user_id, None)
    await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": now()}})

    user = await users_col.find_one({"user_id": user_id}) or {}

    # Clean up group calls
    for group_id, members in list(group_calls.items()):
        if user_id in members:
            group_calls[group_id] = [u for u in members if u != user_id]
            for uid in group_calls[group_id]:
                await send_to(uid, "group_member_left", {
                    "call_id": group_id, "user_id": user_id, "name": user.get("name","?")
                })

    # Clean up 1-to-1 call
    my_active_call = active_calls.pop(sid, {})
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
            await send_to(other_id, "call_ended", {
                "call_id": call_id, "duration": dur,
                "by_name": user.get("name","?"),
                "msg": f"{user.get('name','?')} disconnected"
            })

    await broadcast_event("user_offline", {
        "user_id": user_id, "name": user.get("name","?"), "timestamp": now()
    })
    await broadcast_event("user_free", {"user_id": user_id})


# ── Chat ───────────────────────────────────────────────────────────────────────

@sio.on("chat_message")
async def on_chat_message(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    user = await users_col.find_one({"user_id": user_id}) or {}

    rid      = m.get("receiver_id", "")
    text     = m.get("text", "").strip()
    msg_type = m.get("msg_type", "text")
    media    = m.get("media")
    if not text and not media:
        return

    privacy = await privacy_col.find_one({"from_id": rid, "to_id": user_id})
    if privacy and privacy.get("status") == "blocked":
        await sio.emit("error", {"info": "You are blocked by this user"}, to=sid)
        return

    conv_exists = await messages_col.find_one({"conversation_id": conv_id(user_id, rid)})
    accept_doc  = await privacy_col.find_one({"from_id": rid, "to_id": user_id, "status": "accepted"})

    if not conv_exists and not accept_doc:
        await privacy_col.update_one(
            {"from_id": rid, "to_id": user_id},
            {"$set": {"from_id": rid, "to_id": user_id, "status": "pending",
                      "requester_id": user_id, "requester_name": user.get("name"),
                      "updated_at": now()}},
            upsert=True
        )
        msg_doc = {
            "message_id": str(uuid.uuid4()),
            "conversation_id": conv_id(user_id, rid),
            "sender_id": user_id, "sender_name": user.get("name"),
            "receiver_id": rid, "text": text,
            "msg_type": msg_type, "media": media,
            "timestamp": now(), "read": False, "pending_privacy": True
        }
        await messages_col.insert_one(msg_doc)
        msg_doc.pop("_id", None)
        await send_to(rid, "privacy_request", {
            "from_id": user_id, "from_name": user.get("name"),
            "avatar_color": user.get("avatar_color","#7c3aed"),
            "message_preview": text[:50] if msg_type=="text" else f"[{msg_type}]"
        })
        await sio.emit("message_pending", {"message_id": msg_doc["message_id"]}, to=sid)
        return

    cid2 = conv_id(user_id, rid)
    doc = {
        "message_id": str(uuid.uuid4()), "conversation_id": cid2,
        "sender_id": user_id, "sender_name": user.get("name"),
        "receiver_id": rid, "text": text,
        "msg_type": msg_type, "media": media,
        "timestamp": now(), "read": False,
        "edited": False, "deleted": False
    }
    await messages_col.insert_one(doc)
    doc.pop("_id", None)
    await send_to(rid,     "chat_message", doc)
    await sio.emit("message_sent", doc, to=sid)


@sio.on("privacy_response")
async def on_privacy_response(sid, m):
    user_id      = sid_to_uid.get(sid)
    if not user_id: return
    user         = await users_col.find_one({"user_id": user_id}) or {}
    requester_id = m.get("from_id")
    action       = m.get("action")  # "accept" | "block"

    await privacy_col.update_one(
        {"from_id": user_id, "to_id": requester_id},
        {"$set": {"status": action+"ed", "updated_at": now()}}
    )
    if action == "accept":
        pending = await messages_col.find(
            {"conversation_id": conv_id(user_id, requester_id), "pending_privacy": True}
        ).to_list(100)
        for pm in pending:
            await messages_col.update_one(
                {"message_id": pm["message_id"]}, {"$unset": {"pending_privacy": ""}}
            )
            pm.pop("_id", None)
            pm.pop("pending_privacy", None)
            await sio.emit("chat_message", pm, to=sid)
        await send_to(requester_id, "privacy_accepted", {
            "by_id": user_id, "by_name": user.get("name")
        })
    else:
        await messages_col.delete_many({
            "conversation_id": conv_id(user_id, requester_id), "pending_privacy": True
        })
        await send_to(requester_id, "privacy_blocked", {"by_name": user.get("name")})


@sio.on("edit_message")
async def on_edit_message(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
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
            edit_event = {"message_id": msg_id, "text": new_text, "edited_at": now()}
            await send_to(other_id, "message_edited", edit_event)
            await sio.emit("message_edited", edit_event, to=sid)


@sio.on("delete_message")
async def on_delete_message(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    msg_id  = m.get("message_id")
    msg = await messages_col.find_one({"message_id": msg_id})
    if msg and msg["sender_id"] == user_id:
        created = datetime.fromisoformat(msg["timestamp"])
        if datetime.utcnow() - created <= timedelta(hours=1):
            await messages_col.update_one(
                {"message_id": msg_id},
                {"$set": {"deleted": True, "text": "", "media": None, "deleted_at": now()}}
            )
            other_id = msg["receiver_id"] if msg["sender_id"]==user_id else msg["sender_id"]
            del_event = {"message_id": msg_id}
            await send_to(other_id, "message_deleted", del_event)
            await sio.emit("message_deleted", del_event, to=sid)


@sio.on("typing")
async def on_typing(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    user = await users_col.find_one({"user_id": user_id}) or {}
    await send_to(m.get("receiver_id"), "typing", {
        "from_id": user_id, "from_name": user.get("name"),
        "is_typing": m.get("is_typing", True)
    })


@sio.on("mark_read")
async def on_mark_read(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    cid2 = conv_id(user_id, m.get("other_id",""))
    await messages_col.update_many(
        {"conversation_id": cid2, "receiver_id": user_id, "read": False},
        {"$set": {"read": True}}
    )


@sio.on("messages_read")
async def on_messages_read(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    to_id = m.get("to_id")
    await send_to(to_id, "messages_read", {"from_id": user_id})


# ── 1-to-1 Calls ──────────────────────────────────────────────────────────────

@sio.on("call_request")
async def on_call_request(sid, m):
    user_id   = sid_to_uid.get(sid)
    if not user_id: return
    user      = await users_col.find_one({"user_id": user_id}) or {}
    clee_id   = m.get("callee_id")
    call_type = m.get("call_type", "audio")

    if clee_id in in_call:
        clee = await users_col.find_one({"user_id": clee_id}) or {}
        await sio.emit("call_rejected", {
            "call_id": "busy", "by_name": clee.get("name","?"),
            "busy": True, "msg": "User is in another call"
        }, to=sid)
        return

    call_id  = str(uuid.uuid4())[:12]
    clee     = await users_col.find_one({"user_id": clee_id}) or {}
    call_doc = {
        "call_id": call_id, "caller_id": user_id, "caller_name": user.get("name"),
        "callee_id": clee_id, "callee_name": clee.get("name","?"),
        "status": "ringing", "call_type": call_type,
        "started_at": now(), "ended_at": None, "duration_sec": 0,
        "has_recording": False, "is_group": False
    }
    await calls_col.insert_one(call_doc)

    active_calls[sid] = {"call_id": call_id, "other_id": clee_id}
    in_call[user_id]  = call_id

    await send_to(clee_id, "incoming_call", {
        "call_id": call_id, "caller_id": user_id, "caller_name": user.get("name"),
        "avatar_color": user.get("avatar_color","#7c3aed"),
        "call_type": call_type, "timestamp": now()
    })
    await sio.emit("call_ringing", {
        "call_id": call_id, "callee_name": clee.get("name","?"), "call_type": call_type
    }, to=sid)
    await broadcast_event("user_busy", {"user_id": user_id}, exclude=user_id)


@sio.on("call_accepted")
async def on_call_accepted(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
    user     = await users_col.find_one({"user_id": user_id}) or {}
    call_id  = m.get("call_id")
    caller_id = m.get("caller_id")

    active_calls[sid] = {"call_id": call_id, "other_id": caller_id}
    in_call[user_id]  = call_id
    await calls_col.update_one({"call_id": call_id}, {"$set": {"status": "active"}})
    await send_to(caller_id, "call_accepted", {
        "call_id": call_id, "callee_name": user.get("name"), "callee_id": user_id
    })
    await broadcast_event("user_busy", {"user_id": user_id}, exclude=user_id)


@sio.on("call_rejected")
async def on_call_rejected(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
    user     = await users_col.find_one({"user_id": user_id}) or {}
    call_id  = m.get("call_id")

    in_call.pop(user_id, None)
    await calls_col.update_one(
        {"call_id": call_id}, {"$set": {"status": "rejected", "ended_at": now()}}
    )
    await send_to(m.get("caller_id"), "call_rejected", {
        "call_id": call_id, "by_name": user.get("name")
    })
    active_calls[sid] = {}


@sio.on("call_ended")
async def on_call_ended(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
    user     = await users_col.find_one({"user_id": user_id}) or {}
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
    await send_to(other_id, "call_ended", {
        "call_id": call_id, "duration": dur, "has_recording": has_rec,
        "by_name": user.get("name"), "msg": f"Call ended · {fmt_sec(dur)}"
    })
    await broadcast_event("user_free", {"user_id": user_id}, exclude=user_id)
    active_calls[sid] = {}


# ── Group Calls ────────────────────────────────────────────────────────────────

@sio.on("group_call_invite")
async def on_group_call_invite(sid, m):
    user_id     = sid_to_uid.get(sid)
    if not user_id: return
    user        = await users_col.find_one({"user_id": user_id}) or {}
    invitee_ids = m.get("invitee_ids", [])[:3]
    call_type   = m.get("call_type", "audio")
    group_id    = "grp_" + str(uuid.uuid4())[:10]
    participants = [user_id] + invitee_ids

    group_calls[group_id] = [user_id]
    in_call[user_id]      = group_id

    grp_doc = {
        "call_id": group_id, "host_id": user_id, "host_name": user.get("name"),
        "participants": participants, "joined": [user_id],
        "call_type": call_type, "status": "ringing",
        "started_at": now(), "ended_at": None, "duration_sec": 0, "is_group": True
    }
    await calls_col.insert_one(grp_doc)

    for inv_id in invitee_ids:
        if inv_id in in_call: continue
        await send_to(inv_id, "group_call_invite", {
            "call_id": group_id, "host_id": user_id, "host_name": user.get("name"),
            "avatar_color": user.get("avatar_color","#7c3aed"),
            "call_type": call_type, "participant_count": len(participants)
        })

    await sio.emit("group_call_created", {
        "call_id": group_id, "invitees": invitee_ids, "call_type": call_type
    }, to=sid)


@sio.on("group_call_join")
async def on_group_call_join(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
    user     = await users_col.find_one({"user_id": user_id}) or {}
    group_id = m.get("call_id")

    if group_id in group_calls:
        group_calls[group_id].append(user_id)
        in_call[user_id] = group_id
        await calls_col.update_one(
            {"call_id": group_id},
            {"$addToSet": {"joined": user_id}, "$set": {"status": "active"}}
        )
        for uid in group_calls[group_id]:
            if uid != user_id:
                await send_to(uid, "group_member_joined", {
                    "call_id": group_id, "user_id": user_id, "name": user.get("name")
                })
        await sio.emit("group_call_joined", {
            "call_id": group_id, "members": group_calls[group_id]
        }, to=sid)


@sio.on("group_call_leave")
async def on_group_call_leave(sid, m):
    user_id  = sid_to_uid.get(sid)
    if not user_id: return
    user     = await users_col.find_one({"user_id": user_id}) or {}
    group_id = m.get("call_id")

    in_call.pop(user_id, None)
    if group_id in group_calls:
        group_calls[group_id] = [u for u in group_calls[group_id] if u != user_id]
        remaining = group_calls[group_id]
        for uid in remaining:
            await send_to(uid, "group_member_left", {
                "call_id": group_id, "user_id": user_id, "name": user.get("name")
            })
        if len(remaining) < 2:
            await calls_col.update_one(
                {"call_id": group_id}, {"$set": {"status": "ended", "ended_at": now()}}
            )
            for uid in remaining:
                await send_to(uid, "call_ended", {
                    "call_id": group_id, "msg": "Group call ended"
                })
                in_call.pop(uid, None)
            group_calls.pop(group_id, None)


@sio.on("group_call_reject")
async def on_group_call_reject(sid, m):
    user_id = sid_to_uid.get(sid)
    if not user_id: return
    user    = await users_col.find_one({"user_id": user_id}) or {}
    host_id = m.get("host_id")
    await send_to(host_id, "group_call_declined", {
        "user_id": user_id, "name": user.get("name")
    })


# ── WebRTC Signaling ───────────────────────────────────────────────────────────

@sio.on("webrtc_offer")
async def on_webrtc_offer(sid, m):
    user_id = sid_to_uid.get(sid)
    await send_to(m.get("target_id"), "webrtc_offer", {**m, "from_id": user_id})

@sio.on("webrtc_answer")
async def on_webrtc_answer(sid, m):
    user_id = sid_to_uid.get(sid)
    await send_to(m.get("target_id"), "webrtc_answer", {**m, "from_id": user_id})

@sio.on("ice_candidate")
async def on_ice_candidate(sid, m):
    user_id = sid_to_uid.get(sid)
    await send_to(m.get("target_id"), "ice_candidate", {**m, "from_id": user_id})


# ── Static files ───────────────────────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

static_dir = Path("static")
static_dir.mkdir(exist_ok=True)
fastapi_app.mount("/static", StaticFiles(directory="static"), name="static")

@fastapi_app.get("/")
async def read_index():
    return FileResponse(static_dir / "index.html")