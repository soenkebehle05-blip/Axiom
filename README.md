# Smart Scheduler

**Live demo:** https://smart-scheduler-yyfn.onrender.com (Render free tier; the first load can take about a minute while the instance wakes up.)

A voice assistant that finds and books meeting times on Google Calendar through a spoken, multi-turn conversation.

```
You:  I need to schedule a meeting.
Bot:  How long should it be?
You:  One hour, sometime Tuesday afternoon.
Bot:  Tuesday afternoon is fully booked with quarterly planning and a hiring panel.
      Would Wednesday at 12 or 1 PM work instead?
You:  The first one, book it.
Bot:  Done, Meeting is booked for Wednesday the 30th at 12 PM for an hour.
```

## Setup

Requirements: [uv](https://docs.astral.sh/uv/), a Google account, an API key for one LLM provider (Anthropic, Gemini or any OpenAI-compatible service), and a Deepgram key for speech recognition and synthesis (free credit, no card, at console.deepgram.com). Without Deepgram the app falls back to Chrome's built-in recogniser and voice.

```bash
git clone <repo> && cd smart-scheduler
make setup                  # installs uv if missing, creates .venv, installs dependencies
cp .env.example .env        # add your LLM key and DEEPGRAM_API_KEY
```

Every step below has a `make` target (see the `Makefile`); `make format` runs ruff.

### 1. Google Calendar credentials

1. In the [Google Cloud console](https://console.cloud.google.com), enable the **Google Calendar API**.
2. Configure the **OAuth consent screen** (External) and add your Google account as a test user.
3. Create an **OAuth client ID** of type *Desktop app* and save the download as `credentials.json` in this folder.
4. Authorize once. It writes `token.json`, which the server uses from then on:

   ```bash
   make authorize
   ```

5. Optional: add the demo events (a booked Tuesday afternoon, "Project Alpha Kick-off", a Friday flight, past sync-ups) to your calendar so every scenario has something to work with. `make seed-clean` removes them again.

   ```bash
   make seed
   ```

### 2. Run

```bash
make run                    # make run PORT=9000 to use another port
```

Open http://localhost:8080, click the microphone (or press Space) and talk. There is also a text box.

Visitors can connect their own calendar from the **Calendar** button in the header ("Sign in with Google"). Each browser gets its own calendar; disconnecting reverts to the server's default one. On a public URL this needs a *Web application* OAuth client with `<PUBLIC_BASE_URL>/api/calendar/oauth/callback` registered as a redirect URI, set via `GOOGLE_OAUTH_CLIENT_JSON` and `PUBLIC_BASE_URL`.

### 3. Scenario replay

Replays the assignment's conversations against the live model and your calendar, printing each turn and tool call:

```bash
make scenarios              # make scenarios ARGS="2 5" replays only those scenario numbers
```

### 4. Deploy

**Render:** push to GitHub, create a Blueprint from `render.yaml`, and fill in the secrets it prompts for: `ANTHROPIC_API_KEY`, `CALENDAR_CREDS_JSON` (the one-line JSON printed by `make authorize`) and `DEEPGRAM_API_KEY`. To run on Gemini instead, add `GEMINI_API_KEY` and `LLM_PROVIDER=gemini` as extra environment variables in the Render dashboard.

**Cloud Run (needs a billing account, stays in the free tier):**

```bash
gcloud auth login
make deploy PROJECT_ID=<project> REGION=us-east1
```

The deploy script reads `ANTHROPIC_API_KEY` from `.env` and `token.json` from this folder.

Use a US region: the model APIs are served from the US, so each model call saves about 200 ms of round trip.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `auto` | `anthropic`, `gemini` or `openai`; `auto` picks the first key that is set |
| `ANTHROPIC_MODEL` | `claude-opus-5` | Most accurate in practice. `claude-haiku-4-5` is faster but less reliable |
| `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL` | `gemini-3.5-flash-lite` / `gemini-flash-lite-latest` | Fallback is used automatically on 429/503 for five minutes |
| `OPENAI_BASE_URL` | | Point at OpenRouter, Groq or Ollama for any Chat Completions server |
| `STT_PROVIDER` | `auto` | `deepgram` streams the mic to Nova-3; `browser` uses Chrome's Web Speech API |
| `STT_ENDPOINTING_MS` / `STT_FINAL_GRACE_MS` | `300` / `500` | Silence that ends an utterance, plus a grace period so a mid-sentence pause ("… for Friday") doesn't split it |
| `TTS_PROVIDER` | `auto` | `deepgram` (free credit, no card), `google` (needs billing), `browser` |
| `DEEPGRAM_VOICE` / `TTS_VOICE` | `aura-2-thalia-en` / `en-US-Chirp3-HD-Aoede` | Voice for Deepgram / Google TTS |
| `ACK_ENABLED` / `ACK_DELAY_MS` | `true` / `500` | Play a cached "Okay." shortly after the user stops talking; skipped when the reply is already ready |
| `SPECULATION_ENABLED` | `true` | Start the model on the interim transcript. Doubles model calls on a miss, so `.env.example` turns it off for free tiers |
| `HOLIDAY_CALENDAR_ID` | `auto` | Google's public holiday calendar for the timezone (`none` to disable, or a calendar id) |
| `WORK_DAY_START` / `WORK_DAY_END` | `9` / `18` | Working hours for slot search |
| `DEFAULT_TIMEZONE` | `Asia/Kolkata` | Used when the browser sends none |

Note on the Gemini free tier: `gemini-2.5-flash` allows 5 requests a minute and 20 a day, which is enough for local use but not for a public demo link; that is why the default is `gemini-3.5-flash-lite`.

## How it works

```
Browser                                       FastAPI server
mic (AudioWorklet)  ──── PCM audio ────────▶  Deepgram STT (streaming, 300 ms endpointing)
transcript            ◀── transcript ────  agent loop: LLM ⇄ tools ⇄ Google Calendar
Web Audio playback       ◀── tokens, PCM ───  sentence chunker → TTS (Deepgram / Google)
```

**Agent loop** ([`app/agent/agent.py`](app/agent/agent.py)). Each turn streams a model response with function calling. Text is forwarded as it arrives; tool calls are executed, their results appended to the history, and the model is called again, up to four rounds. The loop is provider-neutral: [`llm_claude.py`](app/agent/llm_claude.py), [`llm_gemini.py`](app/agent/llm_gemini.py) and [`llm_openai.py`](app/agent/llm_openai.py) implement the same small interface (`stream`, `user_message`, `tool_results`, `assistant_message`, `warm`).

**Tools** ([`app/agent/tools.py`](app/agent/tools.py)). Four of them: `find_available_slots`, `find_events`, `create_event`, `remember_preference`. The slot search is pure Python ([`calendar/slots.py`](app/calendar/slots.py)) and, when nothing fits, returns alternatives (following days at the same time, the same day outside the preferred hours, a shorter meeting) so the agent can offer a way out instead of failing. `before_event` / `after_event` parameters let "before my flight" or "two days after the kick-off" be a single call. Public holidays (from Google's regional holiday calendar) are still offered, but every slot on one carries the holiday's name and the agent says it when offering or booking. `create_event` rejects overlaps.

**Prompt** ([`app/agent/prompts.py`](app/agent/prompts.py)). The model does the language; Python does the dates. Every turn the system prompt gets a date sheet (today, the next 14 dates by weekday, this and next week's ranges, the last weekday of the month) and a calendar snapshot with the next two weeks' events and precomputed free blocks. Plain requests like "Tuesday afternoon" are answered from the snapshot without a tool call; the tools handle buffers, deadlines, exclusions and alternatives. The prompt also carries the conversation policy: collect duration and a time window before searching, one question at a time, at most three options, book immediately when the instruction is explicit and the slot is free, ask only on a conflict or a holiday, keep earlier constraints when the user changes one. Availability is presented the way a person would: a frame with nothing in it is reported as "Monday afternoon is free", a frame broken up by meetings gets up to three concrete times, and a frame with many gaps gets a narrowing question instead of three arbitrary times.

**Memory.** Conversation history carries context between turns (the duration given two turns ago still applies). Lasting preferences such as the usual meeting length are retained across sessions for the same browser, isolated from other visitors, and reset on server restart. The agent can also infer the usual duration from past calendar events.

**Voice** ([`app/voice/pipeline.py`](app/voice/pipeline.py), [`stt.py`](app/voice/stt.py)). While the user is listening, the browser streams 16-bit PCM over the WebSocket and the server forwards it to a Deepgram Nova-3 session opened for that window only, so silence between turns is not billed and the assistant's own voice is never transcribed. Interim transcripts feed the display (and speculation); the final one starts the turn. Tokens and reply audio stream back over the same socket. Four things keep replies fast:

- A cached acknowledgement clip ("Okay.") is sent about half a second after the transcript is final, unless the reply is already ready.
- The model says a short bridging sentence before each tool call, released to TTS immediately, so the calendar lookup and second model call happen while the user hears it.
- Bookings and preference saves are confirmed from a template instead of a second model call.
- Optionally, generation starts on the interim transcript and is committed only if the final transcript matches; side-effecting tools wait behind a commit gate so a cancelled guess never books anything.


## Optimizations

Everything below exists to make the conversation feel fast and to keep model calls (and cost) low. In plain words:

**Faster replies** (to target the <800ms mark)

- **Instant "Okay."** Three short clips are synthesised once at startup; one is played a beat after you stop talking, so there is never silence while the model thinks.
- **Talk while working.** Before looking at the calendar, the model says "Let me check your calendar." That sentence is spoken immediately, covering the lookup and the second model call. Before a booking it stays silent instead, because the confirmation is templated and arrives within a second; a stray bridge before a booking is dropped from speech so it never runs into the "Done".
- **Speak sentence by sentence.** Text is sent to TTS as soon as a sentence ends, not when the whole reply is done, so the first sentence plays while the rest is still being written. If the model pauses mid-sentence, the part so far is spoken.
- **Stream everything.** Model text, synthesized audio and mic audio all stream over one WebSocket; audio frames are scheduled back to back on the browser's audio clock so playback has no gaps.
- **Faster end-of-speech.** Deepgram is told to treat 300 ms of silence as the end of a sentence; the browser's built-in recogniser waits about a second.
- **Remember what was said.** With Deepgram, sentences already synthesised (bridges, standard questions) are cached, so repeats are instant.
- **Keep connections warm.** Connections to the model and voice services are opened at startup, re-warmed when a page connects, and kept alive for ten minutes, so no turn pays a TLS handshake.
- **Prefetch the calendar.** The two-week calendar snapshot is loaded when the page connects and refreshed in the background when stale or after a booking. Concurrent reads for the same agent share the refresh lock. The first turn can still wait if prefetch has not finished.
- **Less thinking.** Claude runs with thinking off and low effort; Gemini with the lowest thinking level. Scheduling replies do not need deep reasoning, and this roughly halves time to first word.
- **Guess early (optional).** With `SPECULATION_ENABLED`, the model starts on the interim transcript before you finish; if the final transcript matches, the prepared reply plays immediately and the "Okay." is skipped. Booking waits until the guess is confirmed, so a wrong guess never books anything.

**Fewer model calls** (something I had to strive for given the usage limit on gemini-2.5-flash model) 

- **Calendar in the prompt.** Each turn the prompt includes the next two weeks of events and the free gaps between them, precomputed in Python. "Tuesday afternoon?" is answered by reading, without a tool call. Cached for a minute; refreshed after a booking.
- **Confirmations without the model.** After a booking or a saved preference, the server speaks a templated confirmation instead of asking the model to phrase it, saving one call per booking.
- **One call for anchored requests.** "Before my flight" or "two days after the kick-off" used to need a lookup call and then a search call; the search tool now takes `before_event` / `after_event` and does both.
- **Fall back instead of failing.** On a rate limit or capacity error, Gemini switches to a fallback model for five minutes rather than erroring the turn.
- **Reuse the prompt.** On Claude, the fixed part of the system prompt is marked cacheable so the per-turn date facts do not invalidate it.

**Fewer wrong answers**

- **Dates done in Python.** Today's date, the next 14 dates by weekday, "next week", "late next week" and the last weekday of the month are written into the prompt, so the model never counts days itself.
- **Alternatives when nothing fits.** The slot search returns fallbacks (same time on later days, same day outside preferred hours, a shorter meeting), so the agent can offer a way out instead of saying no.
- **No accidental double bookings.** `create_event` refuses a time that overlaps an existing event unless the user explicitly asked to book over it (the existing event is kept), and refuses a public holiday until the user has heard which holiday it is and said yes.
- **Holiday aware.** Public holidays are marked in the calendar snapshot and labelled on search results, so whenever the agent offers or books a time on one it says which holiday it is. They're only excluded when the model judges the meeting is clearly work and says so.

## Limitations

- End-of-speech is declared after 300 ms of silence (`STT_ENDPOINTING_MS`); shorter cuts off slow speakers, longer adds latency to every turn.
- Visitor calendars and preferences live in memory and reset on restart. A Redis store keyed by the account (the primary calendar's ID, or the OpenID `sub` claim) would make them durable; in-memory is fine for a demo.
- The streaming feels a bit laggy due to free tier models (and also since I am not quite proficient in building chatbots at the moment :) )

## Notes

- The current implementation gives fast responses when deployed near US region since LLM, STT and TTS services are from US region itself causing less network hops.
- I tried many models claude (haiku, sonnet, opus (best results)), gemini (3.x flash, 2.5 flash, 3.5 flash lite) out of which only gemini 3.5 flash lite could fit the requirement cap but still had low reasonability than opus.
- I also tried using gcloud but was not able to setup billing account due to some issues related to my address so for this MVP I went with the beginner friendly render.
- Tests are removed intentionally since they are designed mostly for validating changes and are kept locally.
