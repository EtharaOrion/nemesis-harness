"""LLM adapters that talk to the Claude Code OAuth bridge (src/claude_oauth).

The bridge speaks Anthropic's native ``/v1/messages`` (not the OpenAI
chat-completions shape the other adapters use), so these classes bypass the
inherited OpenAI client and post to the bridge directly. Cache behaviour,
Prompt/Response types and the get_response() contract stay identical to
src/llm/openai.py, so callers can swap adapters without changes.

Start the bridge first:  ./scripts/start_claude_bridge.sh
"""

import httpx

from .invocation import Invocation, Prompt, Response
from .llm_adapter import LLMAdapter
import src.config as conf


class ClaudeBridgeAdapter(LLMAdapter):
    def __init__(self, model: str, read_from_cache: bool = False, save_to_cache: bool = False):
        super().__init__(read_from_cache, save_to_cache, model)
        self.base_url = conf.claude_bridge['url'].rstrip('/')
        self.secret = conf.claude_bridge['secret']
        self.max_tokens = conf.claude_bridge['max-tokens']
        self.timeout = conf.claude_bridge['timeout']
        # Kept for trajectory bundles: the wire response carries token usage and
        # stop_reason that the Response type has nowhere to put.
        self.usages = []
        self.stop_reasons = []

    def _headers(self) -> dict:
        return {
            'content-type': 'application/json',
            # The bridge authenticates on x-api-key and replaces it with the
            # subscription bearer token before forwarding upstream.
            'x-api-key': self.secret or 'nemesis-bridge-stub',
            'anthropic-version': '2023-06-01',
        }

    def _payload(self, prompt: Prompt) -> dict:
        # Anthropic keeps system content out of messages[].
        system = "\n\n".join(m.content for m in prompt.messages if m.role == 'system')
        messages = [
            {'role': m.role, 'content': m.content}
            for m in prompt.messages
            if m.role != 'system'
        ]
        payload = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'messages': messages,
        }
        # Opus 4.8 rejects `temperature` outright ("`temperature` is deprecated
        # for this model", HTTP 400), so it is only sent when a caller opts in
        # for an older model via CLAUDE_BRIDGE_SEND_TEMPERATURE=1.
        if conf.claude_bridge['send-temperature']:
            payload['temperature'] = prompt.temp
        if system:
            payload['system'] = system
        return payload

    def _one_sample(self, payload: dict) -> str:
        r = httpx.post(
            f"{self.base_url}/v1/messages",
            json=payload,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"claude bridge {self.base_url} returned HTTP {r.status_code}: {r.text[:500]}"
            )
        body = r.json()
        self.usages.append(body.get('usage', {}))
        self.stop_reasons.append(body.get('stop_reason'))
        # content[] mixes thinking/text/tool_use blocks; keep the text ones.
        return "".join(
            block.get('text', '')
            for block in body.get('content', [])
            if block.get('type') == 'text'
        )

    def get_response(self, prompt: Prompt) -> Response:
        cached_invocation = self.load_cache(prompt)
        if cached_invocation:
            return cached_invocation.response

        # Anthropic has no `n`; draw sample_size completions sequentially.
        payload = self._payload(prompt)
        response = Response([
            Response.Sample(self._one_sample(payload))
            for _ in range(max(1, prompt.sample_size))
        ])

        self.save_cache(Invocation(prompt, response))
        return response


class Opus_4_8_Bridge(ClaudeBridgeAdapter):
    def __init__(self, read_from_cache: bool = False, save_to_cache: bool = False):
        super().__init__(conf.claude_bridge['model'], read_from_cache, save_to_cache)
