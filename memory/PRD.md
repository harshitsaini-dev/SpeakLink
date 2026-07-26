# EchoCast Live — HQ-to-Store Live Announcement Broadcasting

## Original Problem Statement
Build a separate, independent system that lets HQ operators speak live into a browser microphone and have that voice play in selected store speakers (like an internal FM radio broadcast). MUST NOT touch or depend on any existing EchoGuard AI system.

## Architecture
- **Backend**: FastAPI + SQLAlchemy + SQLite (echocast_live.db), native WebSockets
- **Frontend**: React 19 + Tailwind CSS (IBM Plex Sans / Mono typography, Swiss/Bootstrap-style layout)
- **Audio**: HQ browser MediaRecorder (Opus 32kbps mono, 250ms chunks) → WebSocket binary → server fan-out to selected receivers → receiver MediaSource playback
- **Auth**: JWT (HS256, 8h) for HQ users, opaque UUID receiver_token per store for receivers
- **Deployment target**: Windows 11 local HQ server (uvicorn + built React), no Docker/K8s/Redis

## User Personas
- **HQ Admin**: non-technical retail HQ staff. Opens Broadcast Console, selects stores, speaks into mic.
- **Store Kiosk**: display device (PC/mini-PC/Android box) wired to store speaker/amp; runs receiver page.

## Core Requirements (static)
1. HQ admin login
2. Store list w/ CRUD & online/offline status
3. Target selection: all / selected / region / city / online-only
4. Live broadcast: start / stop / emergency stop
5. Single-broadcast lock (only one live at a time)
6. Broadcast history + per-session detail
7. Receiver kiosk page with one-time Enable Audio (autoplay policy handling)
8. WebSocket real-time commands + audio streaming
9. Auto-stop on broadcaster/HQ disconnect
10. Session logs + system logs

## What's Been Implemented (2026-01-07)
- ✅ Full JWT auth (login, /me, logout) with bcrypt + admin seeded ([REMOVED INSECURE HISTORICAL DEFAULT])
- ✅ 13 sample stores seeded (Mumbai, Pune, Delhi, Gurgaon, Bangalore, Hyderabad, Chennai, Kolkata, Online)
- ✅ Store CRUD, regenerate-token, soft-disable, filters (city/region/status/q)
- ✅ Broadcast session lifecycle (create/start/stop/emergency-stop) + concurrent broadcast lock
- ✅ WebSocket: HQ dashboard status stream, receiver command channel, broadcaster audio uplink
- ✅ Audio pipeline: MediaRecorder → WS binary → fan-out to targeted receivers → MediaSource playback
- ✅ Broadcast history + session detail
- ✅ System logs with level filter
- ✅ Receiver kiosk with Enable Audio, auto-reconnect (exponential backoff), heartbeat every 5s
- ✅ Emergency stop broadcasts to ALL connected receivers (safety net); normal stop only to targets
- ✅ Confirmation modal before starting broadcast; extra warning for All-Stores mode
- ✅ Auto-stop when broadcaster WS disconnects mid-session
- ✅ Force-close active broadcaster on session end
- ✅ SQLite WAL mode, foreign keys enabled

## Testing Status
- Backend: **21/21 pytest cases passing** (`/app/backend/tests/backend_test.py`)
- Frontend: all critical E2E flows passing (Playwright)
- Emergency-stop safety-net verified end-to-end via custom WS test

## Deployment Checklist
### HQ Server (Windows 11)
- Python 3.11+, Node.js 20+
- `pip install -r /app/backend/requirements.txt`
- Configure `/app/backend/.env` (JWT_SECRET, ADMIN_USERNAME, ADMIN_PASSWORD, ECHOCAST_DB_PATH)
- Run backend: `uvicorn server:app --host 0.0.0.0 --port 8001 --app-dir /app/backend`
- Build frontend: `cd /app/frontend && yarn && yarn build`
- Serve frontend + reverse-proxy to backend via nginx (HTTPS **required** — MediaRecorder needs secure context on non-localhost)

### Store Kiosk Devices
- Playback device (PC / mini-PC / Android box / tablet) with modern Chromium/Edge browser
- Wired to store speaker/amplifier via 3.5mm or USB audio interface
- Bookmark: `https://hq-server/receiver?token=<store_token>` (kiosk mode recommended)
- Staff must click "Enable Audio" once after each browser restart

## Backlog / P1
- Server-side WS heartbeat timeout to reset stale 'online' entries
- HQ dashboard WS auto-reconnect (currently receiver-only)
- Redirect / handle stronger contrast on selected store row (design polish)
- HTTPS TLS setup docs for Windows local server

## Backlog / P2 (out of MVP scope)
- Text-to-speech / pre-recorded playlist mode
- Multi-announcer with queuing
- Role-based permissions beyond admin
- Native mobile receiver app
- Compliance / audio-fingerprinting integration (belongs to EchoGuard AI)

## Files (do not modify EchoGuard AI — none present in this repo)
- Backend: `/app/backend/{server,db,models,schemas,auth,seed,ws_manager}.py`
- Frontend: `/app/frontend/src/{App.js, pages/*, components/*, contexts/*, lib/*}`
