<div align="center">

# Agentic World Simulator

**Test a startup's strategy inside a simulated world before trying it in the real one.**

Hundreds of thousands of agents (consumers, companies and stock-market traders) pursue goals, learn and
influence each other in a market calibrated to live news, markets and natural hazards, on an
interactive 3D globe. An AI layer reads the world every day and explains what it means for *your* startup.

![Status](https://img.shields.io/badge/status-student%20project-orange)
![Warranty](https://img.shields.io/badge/warranty-none-red)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
![Python](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)

[Features](#features) · [Screenshots](#screenshots) · [Quick start](#quick-start) · [AI hierarchy](#the-ai-hierarchy) · [Simulation](#how-the-simulation-works) · [License](#license) · [Disclaimer](#disclaimer-and-limitation-of-liability)

</div>

> [!CAUTION]
> **This is a student-grade project, provided "as is", with no warranty and no liability.**
> It was built as a learning exercise. It is not a commercial product, not production-ready, not audited and not maintained.
> Nothing it shows (forecasts, probabilities, simulations, AI-generated text, hazard alerts or insurance hints) is
> financial, investment, legal, insurance, medical or safety advice, and it must not be used for real-world decisions.
> The author(s) accept **no responsibility or liability whatsoever** for any loss or damage arising from its use.
> Released under the [MIT License](LICENSE). Read the full [disclaimer](#disclaimer-and-limitation-of-liability) before using it.

---

## Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Quick start](#quick-start)
- [The AI hierarchy](#the-ai-hierarchy)
- [How the simulation works](#how-the-simulation-works)
- [Architecture](#architecture)
- [Data sources](#data-sources)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [Adding a new prediction type](#adding-a-new-prediction-type)
- [Troubleshooting](#troubleshooting)
- [Built with](#built-with)
- [License](#license)
- [Disclaimer and limitation of liability](#disclaimer-and-limitation-of-liability)

---

## Features

| | |
|---|---|
| **3D globe, light & dark** | Countries coloured by today's market move, the 1-month move, AI-scored news sentiment, news volume or simulated consumer confidence. Earthquakes, cyclones, floods, wildfires and volcanoes appear as alert-coloured points with pulsing rings. Your HQ is marked, and arcs show where your startup could help. |
| **First-run onboarding** | The AI interviews you about the startup: what it sells, whether it can ship and where, its HQ and target markets. Every recommendation is then filtered to what the startup can actually do; a startup with no physical products is never told to ship products and gets market insights instead. |
| **Reliable news, once a day** | 16 feeds from established outlets (CNN, The Times of India, The New York Times, BBC, The Guardian, Al Jazeera, Economic Times and others) are collected once every 24 hours, and fetched before anything else on the very first run. |
| **AI insights with timestamps** | Daily global brief, market outlooks ("the Indian market is down, what happens next?"), region reports, disaster impact analyses, HQ risk alerts with insurance suggestions, "how people are reacting" persona reports and simulation reports. Every one is stored in PostgreSQL with its date and time. |
| **Grounded forecasts** | Market outlooks combine a Monte Carlo forecast on the real index, an agent-based forecast from the calibrated world model, headlines retrieved with pgvector and persona reactions. The AI writes bear/base/bull scenarios that must stay consistent with those numbers. |
| **Disaster impact** | For a significant event the app estimates the local market and currency reaction, what people need, supply-chain risks, and whether your startup can help. A simulation compares helping genuinely, helping promotionally (backlash risk) and not helping. |
| **HQ location risk** | Checks seismic activity near your HQ against its normal rate (a statistical test), the 50-year earthquake history and 7-day weather extremes, then warns you and suggests insurance cover. |
| **Agent-based simulation lab** | Run scenarios such as a market crash, an earthquake plus a relief campaign, a price war or a launch push, with up to 5 million consumer agents across worker processes. Crashes, bubbles, herding and panic cascades emerge from agent interactions and are detected automatically. |
| **Calibrated to reality** | Agent counts and incomes come from World Bank data, starting confidence from news sentiment and persona moods, and each stock-market model is fitted to its real index (volatility, fat tails, volatility clustering). |
| **Live & resilient** | A durable PostgreSQL task queue, a scheduler, live notifications over Server-Sent Events, an AI failover hierarchy and layered spam protection in front of the AI. |

---

## Screenshots

<table>
  <tr>
    <td width="50%"><b>Globe, light theme</b><br>Daily AI brief, market choropleth, HQ and opportunity arcs<br><img src="docs/screenshots/globe-light.png" alt="Globe in light theme with the daily AI brief"></td>
    <td width="50%"><b>Globe, dark theme</b><br>Live markets with sparklines and today's moves<br><img src="docs/screenshots/globe-dark-markets.png" alt="Globe in dark theme with the markets list"></td>
  </tr>
  <tr>
    <td><b>AI market outlook</b><br>Bear/base/bull scenarios tied to Monte Carlo and agent-model numbers<br><img src="docs/screenshots/ai-market-outlook.png" alt="AI market outlook for India"></td>
    <td><b>Market view</b><br>Price history with a Monte Carlo fan (5-95% and 25-75% bands)<br><img src="docs/screenshots/market-forecast-dark.png" alt="NIFTY 50 price history with a Monte Carlo fan chart"></td>
  </tr>
  <tr>
    <td><b>Simulation lab</b><br>200,001 consumer agents and 160,000 trader agents; shaded bands mark emergent crash episodes<br><img src="docs/screenshots/simulation-lab.png" alt="Simulation lab with market and confidence charts"></td>
    <td><b>AI insights</b><br>Every AI result, saved with its date and time<br><img src="docs/screenshots/ai-insights.png" alt="Grid of stored AI insights"></td>
  </tr>
</table>

<p align="center"><img src="docs/screenshots/mobile-dark.png" alt="Phone layout in dark theme" width="260"><br><sub>Phone layout, dark theme</sub></p>

<sub>The screenshots show a fictional sample startup ("Kirana Cloud") and live data from 10 November 2026.</sub>

---

## Quick start

### 1. Configure

```powershell
copy .env.example .env
```

Fill in `.env`:

| Variable | What to put |
|---|---|
| `OPENAI_GEMMA_URL` | the LAN gateway's `base_url`, e.g. `http://192.168.0.101:8000/v1` (several comma-separated URLs allowed) |
| `OPENAI_GEMMA_API_KEY` | the gateway's `api_key` |
| `OPENAI_GEMMA_MODEL` | the gateway's model name, e.g. `gemma` |
| `ANTHROPIC_API_KEY` | optional, enables the Anthropic tier |

Then change the admin login in `config.py` (`ADMIN_USERS`; the default is `admin` / `admin123`).

### 2a. Run everything in Docker

```bash
docker compose up -d --build
```

The first start downloads Gemma 3 4B and EmbeddingGemma (about 4 GB) for the Local Gemma tier.

### 2b. Or run the app natively on Windows

PostgreSQL and Local Gemma stay in Docker; the app itself runs in a Python 3.13 virtual environment.

```powershell
py install 3.13
py -V:3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\start-native.ps1
```

`start-native.ps1` starts the database and Local Gemma containers, stops the Docker copy of the app
(only one can use port 8000) and runs `run.py` in a console window. Close the window to stop it.

> [!NOTE]
> Use Python **3.13**, not 3.15, on Windows. Smart App Control blocks the brand-new 3.15 builds of asyncpg and the
> Anthropic SDK's JSON parser, because Windows has no reputation for them yet. The 3.13 builds load fine.

### 3. Open it

Go to **<http://localhost:8000>** and sign in. On the first run the app collects global headlines,
then asks about your startup.

---

## The AI hierarchy

Every AI request walks this chain, in this order:

```text
  request
     │
     ▼
  1. OpenAI Gemma  ── Gemma 3 12B behind the LAN gateway, called with the `openai` SDK
     │  unreachable / failing
     ▼
  2. Local Gemma   ── Gemma 3 4B in Ollama on this PC (same OpenAI-compatible API)
     │  unreachable / failing
     ▼
  3. Anthropic     ── Claude (claude-opus-5-5) via the `anthropic` SDK, structured JSON output
     │  unreachable / not configured
     ▼
  4. unavailable   ── "AI is not present": the app refuses to start
```

- **At startup** every tier is probed. If none answers, the app does not run and prints each tier's reason.
- **While running**, if every tier drops out the UI locks behind an "AI is not present" screen, background work
  pauses, and everything resumes automatically when a tier comes back.
- **Slow is not absent.** A busy model that times out falls through to the next tier for that request; only an
  unreachable or misconfigured chain locks the app.
- A wrong API key keeps that tier's circuit open for 30 minutes, so the gateway isn't hammered with a bad key.
- If a backend's context window is too small, the request is retried once on the same tier with a condensed prompt.

### Spam protection (AI overload guard)

| Layer | Default |
|---|---|
| Concurrent AI calls | 2; one slot is always kept free for the person using the app |
| Waiting room | 24 queued calls; more are rejected |
| App-wide AI rate | 40 calls per minute (token bucket) |
| Per-admin AI actions | 6 per minute, burst of 3 |
| Identical prompts | merged into one call and cached for 15 minutes |
| Same analysis (e.g. India outlook) | reused if less than 15 minutes old |
| Automatic "market is down" analyses | at most 3 per market refresh, most relevant first |
| Background tasks | de-duplicated: the same job is never queued twice; at most 2 simulations queued |
| Failing tiers | circuit breaker: 3 failures → skipped for 2 minutes |
| Anthropic (paid) | daily call and token budget |
| Inputs | prompts capped at 24k characters, chat messages at 2k |
| API and login | 600 requests per minute per IP; 5 failed logins → 15-minute lockout; CSRF header check |

---

## How the simulation works

Nothing below is scripted: crashes, trends and herd behaviour emerge from agents interacting.

- **Consumers** are vectorised numpy agents on a small-world social network, sharded across worker processes and
  synchronised every simulated day (bulk-synchronous parallel). Each has an income, price sensitivity, social
  susceptibility, a personal contagion threshold, equity exposure, confidence, brand awareness and a memory of
  past experiences with each company.
- **Herding**: panic and euphoria spread through social ties by threshold contagion and recover over time.
  Purchases follow a choice model weighing quality, price against income, awareness, remembered experiences,
  what friends use, and reputation.
- **Companies** set prices and marketing, serve demand within capacity, run campaigns and build reputation;
  adaptive competitors learn pricing and marketing from profit (a multi-armed bandit).
- **Stock markets**: fundamentalist and chartist traders, with opinion herding, trend chasing and strategy
  switching. They are coupled both ways to consumer confidence, so a market crash can spill into spending and
  panicking consumers can drag a market down.
- **Calibration**: each market model is fitted to its real index by the method of simulated moments. Defaults
  already reproduce realistic statistics: about 1% daily volatility, fat tails and volatility clustering.
- **Detected patterns**: crashes, bubbles (with a right-tailed unit-root test), trader herding, consumer panic
  cascades, sentiment regime shifts, volatility clustering, fat tails, adoption tipping points, winner-take-all
  concentration and price wars.

**Measured on the author's 16-core laptop:** 200,001 consumers plus 160,000 traders on 8 worker processes
simulated 120 days in about 5.5 seconds.

---

## Architecture

```text
┌──────────────────────────── Browser ─────────────────────────────┐
│  globe.gl 3D globe · Chart.js · light/dark · live updates (SSE)  │
└──────────────────────────────┬───────────────────────────────────┘
                               │ session cookie · CSRF header · rate limits
┌──────────────────────────────▼───────────────────────────────────┐
│ FastAPI (fully async)                                            │
│  ├─ AI router ─ OpenAI Gemma → Local Gemma → Anthropic → unavail.│
│  │   └─ guard: concurrency · queue · rate · cache · budget       │
│  ├─ intel: onboarding · brief · outlook · geo impact · risk ·    │
│  │         personas · simulation reports                         │
│  ├─ data: news · markets · hazards · World Bank · geography      │
│  └─ task queue (PostgreSQL, SKIP LOCKED) + scheduler             │
└───────┬──────────────────────────────────────────┬───────────────┘
        │ SQLAlchemy 2 async + asyncpg             │ worker processes
┌───────▼──────────────────────────┐  ┌────────────▼───────────────┐
│ PostgreSQL 17 + pgvector         │  │ Simulation engine (numpy)  │
│ insights · news · markets ·      │  │ consumer shards · traders  │
│ hazards · tasks · persona memory │  │ companies · patterns       │
└──────────────────────────────────┘  └────────────────────────────┘
```

---

## Data sources

| Source | Used for | Refresh |
|---|---|---|
| CNN (text-only edition), The Times of India (3 feeds), The New York Times (2), BBC News (2), The Guardian, Al Jazeera, The Economic Times, The Hindu, CNBC, Deutsche Welle, NPR, The Japan Times | headlines for briefs, outlooks and persona agents | every 24 hours |
| Yahoo Finance chart API | 30 indices, volatility gauges, currencies, commodities, Bitcoin | hourly |
| USGS Earthquake Hazards Program | earthquakes and seismic baselines | 15 min / 6 h |
| GDACS (UN / European Commission) | cyclones, floods, volcanoes, droughts, wildfires | 15 min |
| Open-Meteo | geocoding and 7-day weather extremes | 6 h |
| World Bank Open Data | population, GDP per capita, inflation (calibration) | weekly |
| Natural Earth | country shapes on the globe | bundled |

This project is not affiliated with, endorsed by or sponsored by any of these organisations. All names and
trademarks belong to their owners. Respect each source's terms of use.

---

## Configuration

Settings live in [`config.py`](config.py); most can be overridden by environment variables or `.env`.

| Setting | Default | Meaning |
|---|---|---|
| `ADMIN_USERS` | `{"admin": "admin123"}` | the only logins (there are no sign-ups); **change it** |
| `OPENAI_GEMMA_URL` / `_API_KEY` / `_MODEL` | `http://192.168.0.101:8000/v1` · empty · `gemma` | AI tier 1 (LAN gateway) |
| `LOCAL_GEMMA_URL` / `_MODEL` | `http://localhost:11434/v1` · `gemma3:4b` | AI tier 2 (Ollama on this PC) |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | empty · `claude-opus-5-5` | AI tier 3 |
| `AI_STARTUP_POLICY` | `exit` | `exit` refuses to start without AI; `lock` starts with the UI locked |
| `DATABASE_URL` | `postgresql+asyncpg://worldsim:worldsim@localhost:5434/worldsim` | PostgreSQL (host port 5434) |
| `EMBEDDING_MODEL` | `embeddinggemma` | embeddings for pgvector search (local fallback if unavailable) |
| `SIM_SHARDS` | `0` (auto) | simulation worker processes |
| `APP_SECRET_KEY` | built-in dev value | session signing; set your own |

Refresh intervals, spam-protection limits and simulation limits are plain constants in `config.py`.

---

## Project structure

```text
agentic world simulation/
├── app/
│   ├── ai/        AI hierarchy router, spam-protection guard, Gemma + Anthropic providers, embeddings, prompts
│   ├── api/       FastAPI routers (auth, system, world, insights, onboarding, simulation, live stream)
│   ├── data/      news, markets, hazards, World Bank, geography
│   ├── intel/     AI pipelines: onboarding, brief, outlook, geo impact, risk, personas, reports
│   ├── sim/       simulation engine: consumers, companies, traders, engine, patterns, calibration
│   ├── tasks/     PostgreSQL task queue, scheduler, job handlers
│   └── main.py    app setup, middleware, startup AI gate
├── static/        single-page app (HTML, CSS, JS) + vendored globe.gl, Chart.js, textures, country shapes
├── docs/          README screenshots
├── config.py      all settings (admin logins, AI hierarchy, limits)
├── run.py         native entry point (checks the AI hierarchy first)
├── start-native.ps1
├── docker-compose.yml · docker-compose.gpu.yml · Dockerfile
├── requirements.txt
└── .env.example
```

---

## Adding a new prediction type

1. Describe the JSON you want back in `app/ai/prompts.py`.
2. Write the pipeline in `app/intel/`: gather data, call `runtime.ai.generate_json(...)`, store with `save_insight(...)`.
3. Register a job in `app/tasks/jobs.py`, and a schedule in `app/tasks/scheduler.py` if it should run periodically.
4. Optionally expose it through `/api/insights/analyze` and render it in `static/js/render.js`.

---

## Troubleshooting

<details>
<summary><b>The app prints "AI IS NOT PRESENT" and exits</b></summary>

No tier answered. The banner lists each tier's reason. Check the gateway address and key in `.env`, start Local
Gemma with `docker compose up -d ollama ollama-pull`, or set `ANTHROPIC_API_KEY`.
</details>

<details>
<summary><b>The gateway worked yesterday but not today</b></summary>

Its PC may have received a new address from the router. Copy the new `base_url` from the gateway window into
`OPENAI_GEMMA_URL` (you can list several, comma-separated), or give that PC a DHCP reservation in the router.
Local Gemma keeps answering meanwhile.
</details>

<details>
<summary><b>"An Application Control policy has blocked this file" on Windows</b></summary>

Smart App Control is blocking a freshly published compiled package. Use the Python 3.13 virtual environment
described in the quick start, or run everything in Docker.
</details>

<details>
<summary><b>Port 8000 is already in use</b></summary>

Only one copy of the app can run at a time. Use either Docker (`docker compose up -d`) or native
(`start-native.ps1`, which stops the Docker copy for you).
</details>

<details>
<summary><b>AI analyses are slow</b></summary>

Local Gemma on a CPU takes minutes per analysis; the GPU-backed gateway takes seconds. For Local Gemma on an
NVIDIA GPU: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`.
</details>

API documentation is available at <http://localhost:8000/api/docs> after signing in.

---

## Built with

FastAPI · Uvicorn · SQLAlchemy 2 (async) · asyncpg · PostgreSQL + pgvector · NumPy · OpenAI Python SDK ·
Anthropic Python SDK · HTTPX · feedparser · globe.gl / three.js · Chart.js · Ollama · Gemma and EmbeddingGemma ·
Docker Compose. Bundled front-end libraries and data keep their own licences: see
[`static/vendor/THIRD_PARTY_NOTICES.md`](static/vendor/THIRD_PARTY_NOTICES.md).

---

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 DHURJATI-JS.

The software is provided **"as is", without warranty of any kind**, and the authors and copyright holders are
**not liable** for any claim, damages or other liability arising from it or its use. The disclaimer below spells
out what that means for this project. Bundled third-party libraries and data keep their own licences (see
[`static/vendor/THIRD_PARTY_NOTICES.md`](static/vendor/THIRD_PARTY_NOTICES.md)).

---

## Disclaimer and limitation of liability

**Please read carefully. By using, copying, running or modifying this software you accept every term below.**

1. **Student-grade project.** This software was created purely for learning and academic purposes. It is a
   prototype, not a commercial product. It has not been audited, certified, security-tested or validated, and it
   is not maintained or supported.
2. **No warranty.** It is provided **"AS IS" and "AS AVAILABLE", without warranty of any kind**, express or
   implied, including but not limited to warranties of merchantability, fitness for a particular purpose,
   accuracy, reliability, availability, security or non-infringement.
3. **No liability.** To the maximum extent permitted by law, the author(s) and contributors shall **not be liable**
   for any claim, loss or damage of any kind, whether direct, indirect, incidental, special, consequential or
   punitive. This includes financial or trading losses, lost profits or revenue, business interruption, loss of
   data, reputational harm, or personal injury, whether in contract, tort (including negligence) or otherwise,
   arising from or connected with the software, its outputs or its use, even if advised of the possibility of
   such damage.
4. **Not advice.** Nothing produced by this software (market forecasts, probabilities, scenarios, simulation
   results, recommendations, persona reactions, risk levels or insurance suggestions) is financial, investment,
   trading, legal, tax, insurance, medical, safety or any other professional advice. Do not make real decisions
   based on it. Consult qualified professionals.
5. **AI and simulations can be wrong.** AI-generated content may be inaccurate, outdated, biased, incomplete or
   entirely made up ("hallucinated"). Simulations are simplified models of reality built on assumptions; their
   numbers are illustrations, not predictions.
6. **Not a safety system.** Natural-hazard and weather information may be delayed, incomplete or wrong. Never rely
   on it for safety or emergency decisions; always follow official authorities and emergency services.
7. **Third-party data and services.** News, market, hazard and statistical data come from third parties and may be
   delayed, incomplete or incorrect. You are responsible for complying with their terms of use and with the terms
   and costs of any AI service you connect, such as Anthropic API usage. The author(s) are not affiliated with any
   data source or AI provider named in this project.
8. **Security.** The app is designed only for local use on a trusted network. It ships with default credentials
   and is not hardened for exposure to the internet. You are responsible for securing your installation, your
   API keys and your data.
9. **Your responsibility.** You use this software entirely at your own risk and are solely responsible for how you
   use it and for any consequences, including compliance with all applicable laws and regulations.

If you do not agree with these terms, do not use this software.
