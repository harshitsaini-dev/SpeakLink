"""EchoCast Live - main FastAPI application.

This is a standalone module. It does NOT touch or share state with any
existing system. Uses its own SQLite DB (echocast_live.db).
"""
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import List, Optional, Set

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from starlette.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session

from db import engine, get_db, SessionLocal
from models import (
    Base, HQUser, Store, BroadcastSession, BroadcastTarget, ReceiverEvent, SystemLog
)
from schemas import (
    LoginRequest, LoginResponse, UserOut,
    StoreCreate, StoreUpdate, StoreOut, StoresMetaOut,
    SessionCreate, SessionOut, SessionDetailOut, TargetOut,
    ReceiverEventIn, ReceiverVerifyOut,
    SystemLogOut,
)
from auth import verify_password, create_access_token, get_current_user
from seed import seed_admin, seed_stores
from ws_manager import manager

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("echocast")


# ---- app + startup ----
app = FastAPI(title="EchoCast Live", version="1.0.0")


def _write_log(db: Session, level: str, message: str):
    try:
        db.add(SystemLog(level=level, message=message))
        db.commit()
    except Exception:
        db.rollback()


@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_admin(db)
        seed_stores(db)
        _write_log(db, "info", "EchoCast Live server started")
    logger.info("EchoCast Live startup complete")


api = APIRouter(prefix="/api")


@api.get("/")
def root():
    return {"service": "EchoCast Live", "status": "ok"}


# ================ AUTH ================
@api.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(HQUser).filter(HQUser.username == payload.username).first()
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        _write_log(db, "warn", f"Failed login attempt: {payload.username}")
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(user.id, user.username)
    _write_log(db, "info", f"User logged in: {user.username}")
    return LoginResponse(access_token=token, user=UserOut.model_validate(user))


@api.post("/auth/logout")
def logout(user: HQUser = Depends(get_current_user)):
    # JWT is stateless; frontend just discards the token
    return {"ok": True}


@api.get("/auth/me", response_model=UserOut)
def me(user: HQUser = Depends(get_current_user)):
    return UserOut.model_validate(user)


# ================ STORES ================
@api.get("/stores", response_model=List[StoreOut])
def list_stores(
    city: Optional[str] = None,
    region: Optional[str] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user: HQUser = Depends(get_current_user),
):
    query = db.query(Store)
    if not include_inactive:
        query = query.filter(Store.is_active.is_(True))
    if city:
        query = query.filter(Store.city == city)
    if region:
        query = query.filter(Store.region == region)
    if status_f:
        query = query.filter(Store.status == status_f)
    if q:
        like = f"%{q}%"
        query = query.filter((Store.store_name.ilike(like)) | (Store.store_code.ilike(like)))
    # Reflect actual online status from live WS state
    stores = query.order_by(Store.store_code).all()
    online_ids = manager.online_store_ids()
    for s in stores:
        if s.status not in ("playing", "error"):
            s.status = "online" if s.id in online_ids else "offline"
    return stores


@api.post("/stores", response_model=StoreOut, status_code=201)
def create_store(payload: StoreCreate, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    if db.query(Store).filter(Store.store_code == payload.store_code).first():
        raise HTTPException(status_code=409, detail="store_code already exists")
    s = Store(**payload.model_dump(), receiver_token=uuid.uuid4().hex)
    db.add(s)
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Store created: {s.store_code} by {user.username}")
    return s


@api.put("/stores/{store_id}", response_model=StoreOut)
def update_store(store_id: int, payload: StoreUpdate, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Store not found")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(s, k, v)
    db.commit()
    db.refresh(s)
    return s


@api.delete("/stores/{store_id}")
def delete_store(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Store not found")
    s.is_active = False
    db.commit()
    _write_log(db, "info", f"Store disabled: {s.store_code}")
    return {"ok": True}


@api.post("/stores/{store_id}/regenerate-token", response_model=StoreOut)
def regenerate_token(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Store not found")
    s.receiver_token = uuid.uuid4().hex
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Regenerated token for store {s.store_code}")
    return s


@api.get("/stores/meta/regions-cities", response_model=StoresMetaOut)
def stores_meta(db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    regions = [r[0] for r in db.query(Store.region).distinct().order_by(Store.region).all() if r[0]]
    cities = [c[0] for c in db.query(Store.city).distinct().order_by(Store.city).all() if c[0]]
    return StoresMetaOut(regions=regions, cities=cities)


# ================ BROADCAST ================
def _resolve_targets(db: Session, payload: SessionCreate) -> List[Store]:
    q = db.query(Store).filter(Store.is_active.is_(True))
    mode = payload.target_mode
    if mode == "all":
        return q.all()
    if mode == "selected":
        if not payload.store_ids:
            raise HTTPException(status_code=400, detail="store_ids required for target_mode=selected")
        return q.filter(Store.id.in_(payload.store_ids)).all()
    if mode == "region":
        if not payload.region:
            raise HTTPException(status_code=400, detail="region required")
        return q.filter(Store.region == payload.region).all()
    if mode == "city":
        if not payload.city:
            raise HTTPException(status_code=400, detail="city required")
        return q.filter(Store.city == payload.city).all()
    if mode == "online_only":
        return q.filter(Store.is_online_store.is_(True)).all()
    raise HTTPException(status_code=400, detail="Invalid target_mode")


@api.post("/broadcast/sessions", response_model=SessionOut, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    targets = _resolve_targets(db, payload)
    if not targets:
        raise HTTPException(status_code=400, detail="No stores match the selection criteria")
    online_ids = manager.online_store_ids()
    online_count = sum(1 for t in targets if t.id in online_ids)
    session = BroadcastSession(
        campaign_name=payload.campaign_name,
        started_by=user.id,
        status="pending",
        target_mode=payload.target_mode,
        selected_store_count=len(targets),
        online_store_count=online_count,
        offline_store_count=len(targets) - online_count,
        notes=payload.notes,
    )
    db.add(session)
    db.flush()
    for t in targets:
        db.add(BroadcastTarget(session_id=session.id, store_id=t.id, play_status="pending"))
    db.commit()
    db.refresh(session)
    _write_log(db, "info", f"Session created #{session.id} '{session.campaign_name}' targets={len(targets)}")
    return session


@api.post("/broadcast/sessions/{sid}/start", response_model=SessionOut)
async def start_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    if manager.is_live():
        raise HTTPException(status_code=409, detail="A broadcast is already live")
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "pending":
        raise HTTPException(status_code=400, detail=f"Session cannot start (status={session.status})")

    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid).all()
    online_ids = manager.online_store_ids()
    target_store_ids = {t.store_id for t in targets}

    session.status = "live"
    session.started_at = datetime.now(timezone.utc)
    session.online_store_count = sum(1 for sid_ in target_store_ids if sid_ in online_ids)
    session.offline_store_count = len(target_store_ids) - session.online_store_count
    now = datetime.now(timezone.utc)
    for t in targets:
        t.command_sent_at = now
        if t.store_id in online_ids:
            t.play_status = "playing"
            t.started_playing_at = now
        else:
            t.play_status = "failed"
            t.error_message = "Receiver offline at broadcast start"
    db.commit()
    db.refresh(session)

    manager.start_live_session(session.id, target_store_ids)
    # send PLAY to online targets
    for sid_ in target_store_ids:
        if sid_ in online_ids:
            await manager.send_to_receiver(sid_, {"type": "play", "session_id": session.id, "campaign": session.campaign_name})
    await manager.notify_dashboards({"type": "session_started", "session_id": session.id})
    _write_log(db, "info", f"Session #{session.id} started; {session.online_store_count}/{session.selected_store_count} online")
    return session


async def _end_session(db: Session, session: BroadcastSession, final_status: str, reason: str = "", broadcast_to_all: bool = False):
    now = datetime.now(timezone.utc)
    session.status = final_status
    session.ended_at = now
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    for t in targets:
        if t.play_status == "playing":
            t.play_status = "stopped"
            t.stopped_at = now
    db.commit()
    # Broadcast stop to receivers. For emergency stop, notify ALL connected
    # receivers as a safety net; for normal stop, notify only targeted ones.
    if broadcast_to_all:
        stop_ids = set(manager.live_target_store_ids) | set(manager.receivers.keys())
    else:
        stop_ids = set(manager.live_target_store_ids)
    for sid_ in stop_ids:
        await manager.send_to_receiver(sid_, {"type": "stop", "session_id": session.id, "reason": reason})
    manager.stop_live_session()
    # Force-close the active broadcaster WS so its slot is freed immediately
    if manager.active_broadcaster_ws is not None:
        try:
            await manager.active_broadcaster_ws.close(code=1000)
        except Exception:
            pass
    await manager.notify_dashboards({"type": "session_ended", "session_id": session.id, "status": final_status})


@api.post("/broadcast/sessions/{sid}/stop", response_model=SessionOut)
async def stop_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "live":
        raise HTTPException(status_code=400, detail=f"Session not live (status={session.status})")
    await _end_session(db, session, "ended", reason="normal_stop")
    db.refresh(session)
    _write_log(db, "info", f"Session #{session.id} stopped by {user.username}")
    return session


@api.post("/broadcast/emergency-stop")
async def emergency_stop(db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    session = None
    if manager.live_session_id:
        session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
    if session and session.status == "live":
        await _end_session(db, session, "emergency_stopped", reason="emergency", broadcast_to_all=True)
        _write_log(db, "error", f"EMERGENCY STOP triggered by {user.username} on session #{session.id}")
        return {"ok": True, "session_id": session.id}
    # No live session — still broadcast a STOP to all receivers for safety
    for sid_ in list(manager.receivers.keys()):
        await manager.send_to_receiver(sid_, {"type": "stop", "reason": "emergency"})
    _write_log(db, "warn", f"Emergency stop invoked with no live session by {user.username}")
    return {"ok": True, "session_id": None}


@api.get("/broadcast/current")
def current_broadcast(db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    if not manager.live_session_id:
        return {"live": False}
    session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
    if not session:
        return {"live": False}
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    return {
        "live": True,
        "session": SessionOut.model_validate(session).model_dump(mode="json"),
        "targets": [TargetOut.model_validate(t).model_dump(mode="json") for t in targets],
        "online_receivers": list(manager.online_store_ids()),
    }


@api.get("/broadcast/history", response_model=List[SessionOut])
def broadcast_history(limit: int = 50, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    return db.query(BroadcastSession).order_by(BroadcastSession.id.desc()).limit(limit).all()


@api.get("/broadcast/sessions/{sid}", response_model=SessionDetailOut)
def session_detail(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(get_current_user)):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid).all()
    out = SessionDetailOut.model_validate(session)
    out.targets = [TargetOut.model_validate(t) for t in targets]
    return out


# ================ RECEIVER ================
@api.get("/receiver/verify", response_model=ReceiverVerifyOut)
def receiver_verify(token: str, db: Session = Depends(get_db)):
    s = db.query(Store).filter(Store.receiver_token == token, Store.is_active.is_(True)).first()
    if not s:
        return ReceiverVerifyOut(ok=False)
    return ReceiverVerifyOut(ok=True, store=StoreOut.model_validate(s))


@api.post("/receiver/event")
def receiver_event(payload: ReceiverEventIn, db: Session = Depends(get_db)):
    s = db.query(Store).filter(Store.receiver_token == payload.token, Store.is_active.is_(True)).first()
    if not s:
        raise HTTPException(status_code=401, detail="Invalid receiver token")
    db.add(ReceiverEvent(store_id=s.id, event_type=payload.event_type, details=payload.details))
    db.commit()
    return {"ok": True}


# ================ LOGS ================
@api.get("/logs", response_model=List[SystemLogOut])
def list_logs(
    level: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: HQUser = Depends(get_current_user),
):
    q = db.query(SystemLog)
    if level:
        q = q.filter(SystemLog.level == level)
    return q.order_by(SystemLog.id.desc()).limit(limit).all()


# ================ WEBSOCKETS ================
@app.websocket("/api/ws/receiver/{token}")
async def ws_receiver(websocket: WebSocket, token: str):
    # Verify token
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.receiver_token == token, Store.is_active.is_(True)).first()
        if not store:
            await websocket.close(code=4401)
            return
        store_id = store.id
        # mark last_seen
        store.last_seen = datetime.now(timezone.utc)
        store.status = "online"
        db.add(ReceiverEvent(store_id=store_id, event_type="connected"))
        db.commit()
    finally:
        db.close()

    await manager.connect_receiver(store_id, websocket)
    # If a session is currently live and this store is a target -> send PLAY immediately
    if manager.is_live() and store_id in manager.live_target_store_ids:
        await manager.send_to_receiver(store_id, {"type": "play", "session_id": manager.live_session_id})

    try:
        while True:
            msg = await websocket.receive_text()
            # Expect JSON heartbeat / ack / error
            try:
                import json as _json
                data = _json.loads(msg)
            except Exception:
                continue
            evt_type = data.get("type")
            if evt_type == "heartbeat":
                db2 = SessionLocal()
                try:
                    s = db2.query(Store).filter(Store.id == store_id).first()
                    if s:
                        s.last_seen = datetime.now(timezone.utc)
                        db2.commit()
                finally:
                    db2.close()
            elif evt_type in ("play_ack", "stop_ack", "error"):
                db2 = SessionLocal()
                try:
                    db2.add(ReceiverEvent(
                        store_id=store_id, event_type=evt_type, details=str(data.get("details", ""))
                    ))
                    db2.commit()
                finally:
                    db2.close()
                if evt_type == "error":
                    await manager.notify_dashboards({
                        "type": "receiver_error", "store_id": store_id,
                        "details": data.get("details", "")
                    })
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Receiver WS error store={store_id}: {e}")
    finally:
        manager.disconnect_receiver(store_id, websocket)
        db = SessionLocal()
        try:
            s = db.query(Store).filter(Store.id == store_id).first()
            if s:
                s.status = "offline"
                db.add(ReceiverEvent(store_id=store_id, event_type="disconnected"))
                db.commit()
        finally:
            db.close()
        await manager.notify_dashboards({"type": "receiver_status", "store_id": store_id, "status": "offline"})


@app.websocket("/api/ws/hq")
async def ws_hq(websocket: WebSocket, token: str = Query(...)):
    # Verify JWT
    from auth import decode_token
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401)
            return
        user_id = payload["sub"]
    except HTTPException:
        await websocket.close(code=4401)
        return

    hq_id = f"{user_id}:{uuid.uuid4().hex[:8]}"
    await manager.connect_hq(hq_id, websocket)
    try:
        while True:
            # HQ dashboard doesn't send data (yet). This WS is for server->client push.
            msg = await websocket.receive_text()
            _ = msg  # ignored
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"HQ dashboard WS error: {e}")
    finally:
        manager.disconnect_hq(hq_id, websocket)


@app.websocket("/api/ws/broadcaster")
async def ws_broadcaster(websocket: WebSocket, token: str = Query(...)):
    """HQ mic audio uplink. Only one active broadcaster allowed."""
    from auth import decode_token
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401)
            return
    except HTTPException:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    ok = await manager.set_broadcaster(websocket)
    if not ok:
        await websocket.send_text('{"type":"error","message":"Another broadcaster is already active"}')
        await websocket.close(code=4409)
        return

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                await manager.fanout_audio(data)
            # text messages ignored for now
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Broadcaster WS error: {e}")
    finally:
        await manager.clear_broadcaster(websocket)
        # Safety: if session still live when broadcaster drops, auto-stop it
        if manager.is_live():
            db = SessionLocal()
            try:
                session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
                if session and session.status == "live":
                    await _end_session(db, session, "ended", reason="broadcaster_disconnected")
                    _write_log(db, "warn", f"Session #{session.id} auto-stopped: broadcaster disconnected")
            finally:
                db.close()


# Include routes
app.include_router(api)


# CORS - permissive for MVP (LAN deployment)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
