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