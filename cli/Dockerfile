FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install --global @openai/codex@0.160.0 @anthropic-ai/claude-code@2.1.284 && npm cache clean --force
COPY cli/entry.mjs /opt/review-entry.mjs
COPY cli/evidence.mjs /opt/evidence.mjs
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["node", "/opt/review-entry.mjs"]
