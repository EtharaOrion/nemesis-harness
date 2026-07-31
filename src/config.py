import os
from datetime import datetime


def _load_dotenv() -> None:
    """Populate os.environ from a repo-root .env (existing vars win).

    Every setting below is read with os.environ[...] at import time, so the
    file has to be applied before the first lookup. Deliberately dependency
    free: KEY=VALUE lines, '#' comments, optional surrounding quotes.
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

# Claude Code OAuth bridge (src/claude_oauth): an Anthropic-compatible proxy
# that swaps the caller's stub key for the local Claude Code subscription
# token. Start it with scripts/start_claude_bridge.sh.
claude_bridge = {
    'host': os.environ.get('CLAUDE_BRIDGE_HOST', '0.0.0.0'),
    'port': int(os.environ.get('CLAUDE_BRIDGE_PORT', '8765')),
    # Reachable from this process (host side).
    'url': os.environ.get('CLAUDE_BRIDGE_URL', 'http://127.0.0.1:8765'),
    # Reachable from inside containers (OpenHands runtime); command.sh maps
    # host.docker.internal to the host gateway.
    'docker-url': os.environ.get('CLAUDE_BRIDGE_DOCKER_URL', 'http://host.docker.internal:8765'),
    'secret': os.environ.get('WCB_CC_BRIDGE_SECRET', ''),
    'model': os.environ.get('CLAUDE_BRIDGE_MODEL', 'claude-opus-4-8'),
    'max-tokens': int(os.environ.get('CLAUDE_BRIDGE_MAX_TOKENS', '16000')),
    'timeout': float(os.environ.get('CLAUDE_BRIDGE_TIMEOUT', '600')),
    # Opus 4.8 400s on `temperature`; opt in only for models that still take it.
    'send-temperature': os.environ.get('CLAUDE_BRIDGE_SEND_TEMPERATURE', '0') == '1',
}

llm = {
    'llm-invocation-cache-dir': 'cache/llm_invocations',
    'api-url': 'https://openrouter.ai/api/v1',
    'openrouter-api-key': os.environ['OPENROUTER_API_KEY'],
    'default-temp': 0,
    'default-sample-size': 1,
    'default-improvement-iterations': 0,
    'default-model': 'openai/gpt-4.1-nano',
    'max-o4-tokens': 10000,
}

github = {
    'access-token': os.environ['github_access_token'],
}

perf_commit = {
    'max-files': 20, # Taken from PEACE dataset
    'max-likelihood': 90.0,
    'min-exec-time-improvement': 0.05,
    'min-p-value': 0.1,
    'start-date': '2015-01-01',
    'min-stars': 20,
    'max-stars': -1,
}

def get_mvnw_log_file_name(version: str, exec_time: int) -> str:
    return f'logs/{version}_repo_mvnw_{exec_time}.log'

docker = {
    'dockerfile': 'docker/Dockerfile',
    'mvn-settings-file': 'docker/settings.xml',
    'image-name-prefix': 'optds',
    'mvnw-log-path': '/logs',
    'original-repo-path': '/app/original_repo',
    'patched-repo-path': '/app/patched_repo',
    'host-mvnw-log-path': get_mvnw_log_file_name,
    'exec-times': 31,
    'cpu-core-per-exec': 32,
    'memory-per-exec': 80,
    'timeout': 10000,
}

run_analysis = {
    'num-processes': 1,
    'log-file': 'logs/logging_{:%Y-%m-%d-%H-%M}.log'.format(datetime.now()),
    'log-format': '%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
    'log-datefmt': '%H:%M:%S',
    'working-dir': os.environ['workingdir'],
}

data = {
    'dataset-path': 'results/dataset.csv',
}

utils = {
    'working-dir': os.environ['workingdir'],
    'git-extension-dockerfile': 'auxiliary/docker/git_installation_Dockerfile',
}

openhands = {
    'working-dir': os.environ['workingdir'],
    'openhands-files-dir': 'auxiliary/openhands-files',
    'llm-api-key': os.environ['OPENHANDS_API_KEY'],
    # Default routes OpenHands' litellm client at the Claude bridge; with
    # base-url set, 'anthropic/<model>' POSTs to <base-url>/v1/messages and the
    # bridge attaches the subscription token. Override both to go back to
    # OpenRouter (e.g. OPENHANDS_LLM_MODEL=openrouter/z-ai/glm-5.1 with an
    # empty OPENHANDS_LLM_BASE_URL).
    'llm-model': os.environ.get('OPENHANDS_LLM_MODEL', f"anthropic/{claude_bridge['model']}"),
    'llm-base-url': os.environ.get('OPENHANDS_LLM_BASE_URL', claude_bridge['docker-url']),
    'llm-timeout': 3000,
    'max-iterations': 100,
    # Mounted into the OpenHands container so it can start sibling runtimes.
    # Docker Desktop puts the socket at /var/run/docker.sock (a symlink into
    # ~/.docker/run); override for rootless/remote daemons.
    'docker-socket': os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock'),
}

evaluation = {
    'exec-times': 2,
    'min-exec-time-improvement': 0.01,
    'min-p-value': 0.1,
}