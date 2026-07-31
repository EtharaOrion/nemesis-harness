# Claude Code bridge (Opus 4.8 without an API key)

`src/claude_oauth/` is an Anthropic-compatible HTTP proxy that swaps a caller's
stub key for the local **Claude Code subscription** OAuth token, so this harness
can drive `claude-opus-4-8` on-plan instead of through OpenRouter/Bedrock. It is
the same bridge used by WildClawBench, vendored here unchanged.

What it does per request:

- reads the OAuth token from the macOS Keychain service `Claude Code-credentials`
  (or `~/.claude/.credentials.json`), refreshing it when expired,
- replaces the inbound `x-api-key` with `Authorization: Bearer <token>` plus
  `anthropic-beta: oauth-2025-04-20`,
- injects the required `You are Claude Code…` system prefix and the on-plan
  billing-attribution block on `POST /v1/messages`,
- strips third-party fingerprint headers (`user-agent`, `x-stainless-*`) that
  would otherwise route the traffic to metered "extra usage",
- retries 429/529 inline.

## Setup

Everything is already configured in this repo; the pieces are:

| Piece | Path |
|---|---|
| Bridge package | `src/claude_oauth/` |
| Launcher | `scripts/start_claude_bridge.sh` |
| Secrets / URLs / model | `.env` (gitignored) |
| Config surface | `claude_bridge` in `src/config.py` |
| In-process adapter | `src/llm/claude_bridge.py` (`Opus_4_8_Bridge`) |
| OpenHands wiring | `src/config.py` → `openhands`, `auxiliary/openhands-files/config.toml` |

Prerequisites: a logged-in `claude` CLI on this machine and the repo venv
(`uv venv --python 3.13 .venv`; deps via `uv pip install …` or `poetry install`).

## Run it

```bash
./scripts/start_claude_bridge.sh --check    # credential check only
./scripts/start_claude_bridge.sh            # foreground server on :8765
```

Health and a live call:

```bash
curl -s http://127.0.0.1:8765/healthz

curl -s -X POST http://127.0.0.1:8765/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: $WCB_CC_BRIDGE_SECRET" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-4-8","max_tokens":32,
       "messages":[{"role":"user","content":"Reply with exactly: bridge ok"}]}'
```

The bridge binds `0.0.0.0` because the OpenHands runtime container reaches it
through `host.docker.internal`. `WCB_CC_BRIDGE_SECRET` is therefore mandatory —
without it any process on the machine (or LAN) can spend the subscription.
Requests presenting the wrong secret get a 401.

## Using it from the harness

**In-process** (`src/llm/*` adapters):

```python
from src.llm.claude_bridge import Opus_4_8_Bridge
from src.llm.invocation import Prompt

adapter = Opus_4_8_Bridge(read_from_cache=True, save_to_cache=True)
print(adapter.get_response(Prompt([Prompt.Message('user', 'hi')])).first_content)
```

`Opus_4_8_Bridge` keeps the same cache/Prompt/Response contract as the
OpenRouter adapters in `src/llm/openai.py`, so it drops into any call site that
takes an `LLMAdapter`. The pre-existing `src/llm/anthropic.py::Opus_4_8`
(OpenRouter, OpenAI-schema) is untouched.

**OpenHands** (agentic patch/test generation): defaults now render

```toml
model    = "anthropic/claude-opus-4-8"
base_url = "http://host.docker.internal:8765"
api_key  = "<WCB_CC_BRIDGE_SECRET>"
```

into `auxiliary/openhands-files/config.toml`. litellm's `anthropic/` provider
POSTs to `<base_url>/v1/messages`, which is exactly what the bridge serves. Start
the bridge before launching a run.

To go back to OpenRouter for a run:

```bash
OPENHANDS_LLM_MODEL=openrouter/z-ai/glm-5.1 OPENHANDS_LLM_BASE_URL= python main.py …
```

## Knobs

`.env` values, all optional except the secret:

| Variable | Default | Purpose |
|---|---|---|
| `WCB_CC_BRIDGE_SECRET` | — | Shared secret; clients send it as `x-api-key` |
| `CLAUDE_BRIDGE_HOST` / `_PORT` | `0.0.0.0` / `8765` | Listen address |
| `CLAUDE_BRIDGE_URL` | `http://127.0.0.1:8765` | Host-side URL for in-process adapters |
| `CLAUDE_BRIDGE_DOCKER_URL` | `http://host.docker.internal:8765` | Container-side URL for OpenHands |
| `CLAUDE_BRIDGE_MODEL` | `claude-opus-4-8` | Model id forwarded upstream |
| `CLAUDE_BRIDGE_MAX_TOKENS` | `16000` | `max_tokens` on `/v1/messages` |
| `CLAUDE_BRIDGE_SEND_TEMPERATURE` | `0` | Opus 4.8 400s on `temperature`; opt in for older models |
| `WCB_CC_BILLING_ATTRIBUTION` | `1` | On-plan billing block; off ⇒ "extra usage" 400s |
| `WCB_BRIDGE_STREAM_READ_TIMEOUT` | `600` | Per-chunk read budget for long thinking turns |

## Caveats

- Subscription rate/usage caps apply. `GET /quota` reports exhaustion state and
  the next reset; the bridge supports multi-account failover via
  `WCB_CC_ACCOUNT_POOL` (see `src/claude_oauth/credentials.py`).
- The refresh cache is shared with the WildClawBench setup
  (`~/.cache/wildclawbench/claude_creds.json`), so both stay on one token.
- Routing subscription traffic through non-CLI tooling is a grey area under
  Anthropic's consumer ToS — same caveat as the WildClawBench setup.
