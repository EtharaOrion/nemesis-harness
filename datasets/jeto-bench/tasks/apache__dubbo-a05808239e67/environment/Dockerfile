# JETO-Bench task environment.
# The upstream image ships both /app/original_repo (pre-improvement) and
# /app/patched_repo (the real performance commit). The reference solution is
# removed here so the task is not gameable by reading it.
FROM ghcr.io/khesoem/dubbo-a05808239e67174ceefc6e7e5952de5173c23a36:latest

# git: for the agent and for reverting edits. python3: the base image is a bare
# JDK, and tests/verifier.py (stdlib only) needs an interpreter.
RUN apt-get update && apt-get install -y --no-install-recommends git python3 \
    && rm -rf /var/lib/apt/lists/* || true

RUN rm -rf /app/patched_repo

# Pristine baseline captured before the agent can touch anything, so timing does
# not depend on the repo carrying a usable .git.
RUN cp -r /app/original_repo /app/baseline_repo

WORKDIR /app/original_repo
