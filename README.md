# STE BuildCare

A voice-based building complaint system for residents, plus an admin dashboard for building management. Residents call "Maya" (an AI voice assistant powered by Gemini Live) to report issues by talking naturally; complaints are logged automatically to DynamoDB. Admins review, filter, and resolve complaints through a web dashboard, with rule-based pattern detection surfacing things like recurring issues or spikes in a category.

This README is written for someone joining the project cold. If something here goes stale, please update it — a wrong README is worse than no README.

---

## 1. What this actually is

- **Residents** open a web page, log in, and tap a button to start a voice call with Maya. They speak naturally (multiple languages supported); Maya asks clarifying questions, then saves a complaint (type, description, location) to the database and reads back a reference number.
- **Admins** log into a separate view of the same app, see every complaint in a filterable/searchable table, update statuses, and see "Insights" — automatically-detected patterns across complaints (e.g. 5 aircon complaints on the same floor in 2 weeks).
- Everything (signup/login, resident view, admin view) is now **one single-page React app** with client-side routing — it used to be three separate HTML pages, but was unified partway through development. See section 4.

---

## 2. Architecture

```
Browser (mic) ──PCM audio (WSS)──▶ FastAPI /gemini-ws ──▶ Gemini Live API
Browser (speaker) ◀──PCM audio (WSS)── FastAPI /gemini-ws ◀── Gemini Live API

Browser (React SPA) ──HTTPS──▶ FastAPI (auth, complaints, patterns) ──▶ DynamoDB
```

**Voice pipeline** (`backend/gemini_live.py`):
- Browser captures mic audio via an `AudioWorklet` (16kHz PCM Int16), streams it over a WebSocket to the backend.
- Backend proxies raw audio frames straight through to Gemini Live (`gemini-3.1-flash-live-preview`), which does VAD + speech-to-text + LLM + text-to-speech natively in one connection — there's no separate STT/LLM/TTS pipeline.
- Gemini's audio reply (24kHz PCM) streams back through the same WebSocket to the browser, which plays it via Web Audio API.
- "Barge-in" (interrupting Maya mid-sentence): Gemini detects the user talking and sends an interrupt signal; the backend forwards a `{"type":"interrupted"}` message so the browser instantly stops all queued audio.
- When Gemini decides it has enough info, it calls a `save_complaint` tool function; the backend writes to DynamoDB and sends the complaint ID back to both Gemini (to read aloud) and the browser (to display).

**Auth** (`backend/auth.py`):
- Custom email/password auth — NOT using AWS Cognito or any third-party auth provider.
- Passwords are bcrypt-hashed. Sessions are stateless JWTs (7-day expiry), stored client-side in `localStorage` under the key `buildcare_auth` as `{token, email, role}`.
- **Signup grants access immediately** — no email verification, no admin-approval queue. Choosing "Admin" at signup makes you an admin instantly. This was a deliberate simplification made mid-project (earlier versions had OTP email verification and an admin-approval queue; both were built, then explicitly ripped out — see section 8 for why this matters and what to do about it before a real public launch).
- A JWT is also passed as a query param on the `/gemini-ws` WebSocket connection so complaints can be tagged with the resident's email.

**Admin pattern detection** (`backend/patterns.py`):
- Deliberately simple, rule-based, no ML. Three alert types:
  - **Cluster**: 3+ same-type complaints, same location, within 14 days.
  - **Recurring**: 2+ same-type complaints, same location, spread over up to 90 days (but not tight enough to be a cluster) — suggests a past fix didn't hold.
  - **Spike**: a complaint category this month running 2x+ its recent monthly average.
- Location matching normalizes "Block 204" style references via regex; free-text locations fall back to first-few-words matching. This means "2nd floor" and "second floor" are currently treated as *different* locations — a known rough edge, not a bug per se.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI, uvicorn |
| Voice AI | Google Gemini Live (`google-genai` SDK) |
| Database | AWS DynamoDB (two tables — see section 6) |
| Frontend | React 19, Vite, React Router |
| Auth | Custom (bcrypt + PyJWT), no third-party auth service |
| Hosting | AWS EC2 (single t3.small instance), nginx, systemd |
| DNS/CDN | Cloudflare (for the custom domain) |
| Fonts | Plus Jakarta Sans (design system: slate + green accent — see `styles.css`) |

No Docker, no Kubernetes, no message queue, no Redis. This is intentionally a small, simple, single-server deployment — don't over-engineer additions to it without a real reason.

---

## 4. Repo structure

```
backend/
  main.py            FastAPI app: all routes, CORS, the X-API-Key middleware
  auth.py             Email/password auth: signup, login, JWT issuing/verification
  gemini_live.py       The /gemini-ws WebSocket handler — the actual voice pipeline
  dynamodb.py          All DynamoDB reads/writes for complaints
  patterns.py          Cross-complaint pattern detection (Cluster/Recurring/Spike)
  requirements.txt     Runtime deps
  requirements-dev.txt Adds pytest for testing
  conftest.py          Test path setup
  tests/               pytest suite (currently just patterns.py, which is the
                       one piece of business logic complex enough to warrant it)
  .env                 Secrets — NOT committed, see section 7

frontend/
  src/
    main.jsx            Entry point — mounts <App />
    App.jsx              React Router setup: /, /login, /user, /admin + auth guards
    Splash.jsx            Landing splash screen (redirects to /login after ~2s)
    LoginApp.jsx           Login/signup form (email regex, password strength meter)
    CallApp.jsx            The resident voice-call screen (this used to be the
                          entire app before routing was added — still named
                          generically "App" internally in one spot, that's fine)
    AdminApp.jsx           Admin dashboard shell (topbar, walkthrough, logout)
    passwordStrength.js    Shared email/password validation helpers
    useTour.js             Hook for the first-time guided walkthrough (tracks
                          "seen" state against the ACCOUNT via the backend, not
                          localStorage — see section 8)
    styles.css             ALL styling for the whole app lives in this one file.
                          It's long. Sections are marked with banner comments.
    components/
      ComplaintsDashboard.jsx   The actual admin complaints table/filters
      MyComplaints.jsx           Resident's own complaint history (full-screen
                                overlay, toggled by a button — not a route)
      Walkthrough.jsx             Reusable spotlight-tooltip guided-tour component
  index.html            Single HTML entry point (was 3 separate files earlier)
  vite.config.js         Standard single-page Vite config (was multi-page earlier)
  .env / .env.local       Local dev config — NOT committed
  .env.production         Used only when building for deployment — NOT committed
```

**Note on git history**: this app went through several real architecture changes in one continuous session — three separate HTML pages → one SPA with routing; OTP email verification → removed; admin-approval queue → removed; a "cinematic" dark-purple redesign → scrapped and rebuilt on a plainer, more disciplined design system after it read as generic/AI-generated. If you're reading old commit messages or comments that mention things not in this README, the README wins — it reflects current reality.

---

## 5. Local development setup

**Prerequisites**: Python 3.11+, Node 18+, an AWS account with DynamoDB access, a Google AI Studio API key (for Gemini Live).

### Backend

```bash
cd backend
python3 -m venv env && source env/bin/activate
pip install -r requirements-dev.txt   # includes pytest
```

Create `backend/.env` (never commit this):

```
GOOGLE_API_KEY="<your Gemini API key>"
API_KEY="<any random string — shared secret gating most REST endpoints>"
AWS_ACCESS_KEY_ID=<...>
AWS_SECRET_ACCESS_KEY=<...>
AWS_REGION=ap-southeast-1
DYNAMODB_TABLE_NAME=BuildCareComplaints
JWT_SECRET=<random hex string — generate with: python3 -c "import secrets; print(secrets.token_hex(32))">
```

Run it:
```bash
python3 -m uvicorn main:app --host 0.0.0.0 --port 8001
```

### Frontend

```bash
cd frontend
npm install
```

`frontend/.env.local` (already gitignored):
```
VITE_API_URL=http://localhost:8001
VITE_API_KEY=<must match backend's API_KEY exactly>
```

Run it:
```bash
npm run dev    # http://localhost:5173
```

### First login

There's no seed script anymore — just sign up. Choosing "Admin" at signup grants admin access immediately (see section 8 for why this is a real security tradeoff, not an oversight).

### Tests

```bash
cd backend
source env/bin/activate
pytest
```

Only `patterns.py` has tests right now — it's the one module with actual non-trivial logic (date-window math, grouping rules). The rest of the backend is straightforward CRUD/auth glue; add tests if you add logic complex enough to deserve them, don't feel obligated to test everything for its own sake.

---

## 6. Database (DynamoDB)

Two tables, both in `ap-southeast-1`:

**`BuildCareComplaints`** — partition key `complaint_id` (e.g. `CMP-A1B2C3D4`)
| Field | Notes |
|---|---|
| `complaint_id` | `CMP-` + 8 random hex chars |
| `email` | Resident's account email — added partway through development; older rows predate this and won't have it |
| `name`, `complaint_type`, `description`, `location`, `language` | Free text/categorical, captured during the call |
| `status` | `Open` \| `In Progress` \| `Resolved` \| `Closed` |
| `timestamp` | String, format `"YYYY-MM-DD HH:MM:SS SGT"` — note this is a **string**, not a native DynamoDB timestamp type, so all date filtering happens in application code (see `patterns.py`'s `_parse_ts`) |

**`BuildCareUsers`** — partition key attribute named `username`, but it actually holds the account's **email**
| Field | Notes |
|---|---|
| `username` | Holds the email address (see note below) |
| `password_hash` | bcrypt |
| `role` | `user` \| `admin` |
| `created_at` | Unix timestamp |
| `seen_tour_user`, `seen_tour_admin` | Booleans, set once each account dismisses that first-time walkthrough |

**Why the key is called `username` but holds an email**: the auth system was originally username-based, then switched to email-based identity later. Rather than migrate the table (destructive, and blocked by this environment's safety guardrails anyway), the code just stores email strings in the `username` key attribute. If you're ever doing a clean rebuild, name it `email` properly instead — this is debt, not a design choice worth preserving.

---

## 7. Environment variables reference

Never commit `.env` files — `.gitignore` already excludes `*.env`, `backend/.env`, `frontend/.env*`.

| Var | Where | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | backend | Gemini Live API access |
| `API_KEY` | backend + frontend (`VITE_API_KEY`) | Shared secret gating most REST endpoints (not `/auth/*`, not `/health`) — must match exactly between frontend and backend |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` | backend | DynamoDB access |
| `DYNAMODB_TABLE_NAME` | backend | Complaints table name (defaults to `BuildCareComplaints` if unset) |
| `JWT_SECRET` | backend | Signs/verifies login session tokens — **must be set in every environment** (local, EC2, anywhere) or auth silently breaks |
| `VITE_API_URL` | frontend | Backend base URL. **Leave unset in `.env.production`** — the frontend falls back to `window.location.origin` at runtime, which is what lets the same build work correctly on multiple domains (the IP, and any custom domain) without a rebuild per domain |

If you ever see `ws://localhost:8001` errors in a *deployed* build's console, it means `VITE_API_URL` got set to a literal value in `.env.production` again — this exact bug happened once already during development. Leave it unset there.

---

## 8. Known rough edges / deliberate tradeoffs (read before extending)

1. **Signup grants access instantly, including admin.** There is no email verification and no admin-approval step — both were built, then explicitly removed to simplify the demo/testing flow. Anyone who signs up choosing "Admin" gets full admin access immediately. **This is fine for internal testing, not fine for a real public launch.** Before opening this up beyond trusted users, either restore an approval gate, restrict admin signup to an invite code, or provision admins out-of-band.

2. **The `/gemini-ws` WebSocket authenticates two different ways at once**: a shared `api_key` query param (same secret as the REST API) AND an optional per-user JWT `token` query param (used only to tag complaints with the resident's email, not to gate access to the endpoint itself). Don't assume the JWT is doing access control there — it isn't; the shared `api_key` is.

3. **Location-based pattern matching has a real gap**: "2nd floor" and "second floor" are treated as different locations by `patterns.py`'s regex, so genuinely-related complaints can end up in separate alerts instead of one. Worth fixing if pattern detection accuracy matters more than it currently does.

4. **Tamil audio input is sometimes misrecognized** by Gemini Live specifically for spoken (not typed) Tamil — upstream model behavior, not something in our code. All other supported languages (English, Malay, Mandarin, Thai, Indonesian, Vietnamese, Singlish) are reliable.

5. **The walkthrough "seen" flag lives on the account** (DynamoDB `seen_tour_user`/`seen_tour_admin`), not in `localStorage` — this was a deliberate late change specifically so it behaves correctly across devices/browsers once deployed. If you ever add more onboarding flows, follow this pattern rather than reaching for `localStorage`.

6. **`styles.css` is one large file** covering the entire app (auth pages, call screen, admin dashboard, walkthrough, everything). It's organized with banner comments (`/* ─── Section name ─── */`) rather than split into files. Search for the relevant banner rather than assuming a component's styles live near its own `.jsx` file.

7. **No automated frontend tests.** Manual testing has been the practice throughout — for the voice feature specifically, testing via typed text shortcuts gives misleadingly fast/clean results, since it skips Gemini's actual audio understanding. Test with real synthesized/recorded audio through the actual WebSocket when validating voice behavior changes.

---

## 9. Deployment

Current production setup: a single EC2 instance (t3.small, `ap-southeast-1`), nginx serving the built frontend + reverse-proxying API/WebSocket traffic to a `systemd`-managed uvicorn process, Cloudflare in front for DNS/domain/SSL.

**Before deploying a build that includes the auth system**, confirm on the server:
- `bcrypt` and `pyjwt` are installed (`pip install -r requirements.txt`) — the backend **will fail to start** without them, since `auth.py` imports them at module load.
- `JWT_SECRET` is set in the production `.env`.
- The EC2 instance's IAM role has DynamoDB permissions for **both** `BuildCareComplaints` and `BuildCareUsers` — the original role was scoped to only the complaints table before the auth system existed; a new operation needing a new permission will 500 with `AccessDeniedException` in the logs even when the code is otherwise correct. This has bitten this project before — check IAM policy scope whenever adding a new DynamoDB table or operation.

**Deploy backend**: copy changed `.py` files (and `requirements.txt` if dependencies changed) to the server, then restart the service. Use `SIGKILL` rather than a graceful stop — long-running WebSocket connections make graceful shutdown hang.

**Deploy frontend**: `npm run build` locally (needs `.env.production` with `VITE_API_KEY` set, `VITE_API_URL` left unset — see section 7), then sync the single `dist/` output to nginx's web root(s) on the server. Since the frontend is now one unified SPA, the same build works correctly regardless of which domain/IP serves it — no per-domain rebuild needed.

**Nginx**: uses `try_files $uri $uri/ /index.html;` so all client-side routes (`/login`, `/user`, `/admin`) fall back to the single `index.html` and React Router takes over from there.

---

## 10. API reference (quick index)

All endpoints except `/`, `/health`, `/docs`, `/redoc`, `/openapi.json`, anything starting with `/ws` or `/auth`, and `/my-complaints`, require an `X-API-Key` header (or `Authorization: Bearer <API_KEY>`) matching the backend's `API_KEY`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| WS | `/gemini-ws` | `api_key` query param (+ optional `token`) | The voice call itself |
| POST | `/auth/signup` | none | Create account — instant access, see section 8 |
| POST | `/auth/login` | none | Returns a JWT |
| GET | `/auth/me` | JWT | Current identity + tour-seen flags |
| POST | `/auth/tour-seen` | JWT | Mark a walkthrough as dismissed for this account |
| GET | `/my-complaints` | JWT | Resident's own complaint history |
| GET | `/complaints` | X-API-Key | All complaints (admin dashboard) |
| GET | `/complaints/patterns` | X-API-Key | Cluster/Recurring/Spike alerts |
| PATCH | `/complaints/{id}/status` | X-API-Key | Update one complaint's status |
| DELETE | `/complaints/clear?confirm=true` | X-API-Key | **Irreversibly deletes every complaint** — requires the explicit `confirm=true` query param as a guard rail |

Full interactive docs (once the backend is running): `http://localhost:8001/docs`.

---

## 11. Who to ask / where to look next

There's no formal issue tracker referenced in this repo at time of writing. If one gets set up, link it here. For now, `git log` is the most reliable source of *why* something changed — commit messages on this project have generally explained the reasoning, not just the diff.
