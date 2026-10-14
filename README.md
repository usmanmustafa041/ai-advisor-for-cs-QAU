# QAU CS Advisor

A production-oriented assistant for the **Department of Computer Sciences, Quaid-i-Azam University**.
It answers students' questions from the department website (<https://cs.qau.edu.pk>) and the PDFs it
links to — courses, prerequisites, schemes of study, faculty, admissions, fees, the academic calendar,
timetables and regulations — in English, Urdu and Roman Urdu, with citations.

**Start here:** use the Docker Compose setup below to run the full portal, then see
[docs/ADVISOR.md](docs/ADVISOR.md) (architecture, operations, admin guide, limits) and
[docs/PREREQUISITES.md](docs/PREREQUISITES.md) (prerequisite chains, data sources, adding schemes).

| Part | Where |
|---|---|
| Chatbot engine: ingestion, retrieval, answering, API, admin | `backend/advisor/` (Python ≥ 3.12) |
| **Portal interface (the one students and admins use)** | `frontend/` (React + Vite) — its Chat page and *Chatbot Engine* admin page are served by the engine |
| Portal accounts, study plan, fees, timetable pages | `backend/app/` (legacy portal API, unchanged) |
| Standalone minimal UI (optional, no portal needed) | `web/` |
| Tests | `backend/tests_advisor/` (214), `frontend/src/**/*.test.jsx` (33), `web/src/*.test.js` (6) |
| Evaluation sets | `backend/advisor/eval/golden.json`, `golden_holdout.json`, `golden_prereq.json` |
| Deployment | `deploy/advisor/` (Docker, systemd, nginx) |

The portal includes Chat, Study Plan, Courses, Fees, Timetable, Policies, History, and an admin panel.
Its colors and university branding follow the QAU Computer Sciences department site. The advisor engine
is reached through `/advisor/*` so it never collides with the portal's own `/api/v1` routes.

## Quick start (Docker Compose)

Requirements: Git and Docker Desktop (Windows/macOS) or Docker Engine with the Compose v2 plugin
(Linux). Docker Compose builds the portal, API, advisor engine, and PostgreSQL database together.
The first build downloads the Python dependencies and language models and can take several minutes.

```text
git clone https://github.com/usmanmustafa041/ai-advisor-for-cs-QAU.git
cd ai-advisor-for-cs-QAU
```

Optionally create a local environment file from the committed template; never commit real credentials:

```powershell
Copy-Item .env.example .env
```

```bash
cp .env.example .env
```

Change `POSTGRES_PASSWORD` and `AUTH_SECRET` in `.env` before using the stack beyond a local demo.
Then build and start the application from the repository root:

```bash
docker compose up --build -d
docker compose ps
```

Open <http://localhost>. The API health check is at <http://localhost/api/v1/health>; the FastAPI
documentation is at <http://localhost/docs>. To stop the services while preserving the database and
conversation data, run `docker compose down`.

The assistant can return matching department passages without a configured language model. To run the
optional local Ollama model instead, start the `local-llm` profile and download the model once:

```bash
docker compose --profile local-llm up --build -d
docker compose exec ollama ollama pull qwen3:8b
```

Alternatively, administrators can configure a hosted provider from **Chatbot Engine → AI providers**.
For local development without Docker, see [backend/README.md](backend/README.md),
[frontend/README.md](frontend/README.md), and [scripts/run-all.sh](scripts/run-all.sh).

## Measured quality

Questions were written from verified source text (never from the bot's own output) and graded
automatically: every required fact must appear in the answer, forbidden claims must not, and
unanswerable questions must be declined. Run with the **local `qwen3:8b` model only** (the weakest
supported configuration — no Gemini key was available while building).

| Set | Questions | End-to-end pass |
|---|---|---|
| General — tuning set (used while developing) | 109 | 108 (99.1%) |
| General — **held-out** (written afterwards, never tuned on) | 40 | 39 (97.5%) |
| Prerequisite chains (written once the engine was designed, then tuned against) | 24 | 24 (100%) — *optimistic; treat as a regression suite* |

The misses: `cal-07` ("when will results be declared?" — the model answered with admission merit-list
dates instead of the exam result date, a real miss) and `h-37` (the bot correctly declined, in wording
the answer grader did not recognise). Across earlier runs a few other questions flipped between pass and
fail (sampling at temperature 0.2), so treat each figure as ±2 questions.

Capacity of the retrieval stage on this laptop (M1 Max, CPU): ~2.4 questions/s, ≈0.4 s CPU each;
hosted-LLM generation scales independently. Median end-to-end latency with the local model is ~12 s (prompt processing dominates); cached repeats are
instant. Hosted Gemini is expected to be faster and better at long lists and Urdu — but that was
**not measured here**; run `python -m advisor.cli eval-answers` after adding a key to measure it.
