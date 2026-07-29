# syntax=docker/dockerfile:1.7
FROM node:20-alpine AS deps
WORKDIR /app
COPY package*.json .npmrc ./
# The GitHub Packages token is provided as a BUILD SECRET, not a build ARG.
# It is mounted into this RUN's filesystem only, never printed to the build log
# and never persisted in an image layer or `docker history`. The value is read
# from the mount and appended to a temp .npmrc that is deleted in the same layer
# — it is not echoed. `id=npm_token` matches the secret wired in docker-compose.
RUN --mount=type=secret,id=npm_token \
    cp .npmrc .npmrc.auth \
 && printf '//npm.pkg.github.com/:_authToken=%s\n' "$(cat /run/secrets/npm_token)" >> .npmrc.auth \
 && npm ci --userconfig .npmrc.auth \
 && rm -f .npmrc.auth

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* must be present at BUILD time, not run time.
#
# Next.js inlines `process.env.NEXT_PUBLIC_*` into the generated JS bundle during
# `npm run build`, and every page here is a client component ("use client"), so
# these values end up in code that runs in the BROWSER. A runtime `environment:`
# entry in docker-compose therefore has NO effect on them — the bundle was already
# frozen at build time. They must be build args, which is what these ARGs are for.
#
# Consequence to be aware of: the dashboard image is environment-specific. To
# change these you REBUILD, you do not just restart with new env vars.
#
# Defaults are RELATIVE paths on purpose. In production the dashboard and backend
# sit behind one reverse proxy on one hostname (https://host/ -> dashboard,
# https://host/api -> backend, https://host/ws -> backend), so relative values are
# correct everywhere and no hostname is baked into the image. Override for a
# split-origin deployment, e.g.:
#   --build-arg NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_API_URL=/api
# WS path (not a full URL): lib/useWebSocket.ts resolves it against the page
# origin at runtime, which is also what upgrades ws:// to wss:// on an HTTPS page.
# An absolute ws(s):// URL is still accepted there for split-origin setups.
ARG NEXT_PUBLIC_WS_URL=/ws
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./package.json
COPY --from=builder /app/node_modules ./node_modules
EXPOSE 3000
CMD ["npm", "start"]