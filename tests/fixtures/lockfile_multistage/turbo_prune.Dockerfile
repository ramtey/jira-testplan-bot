# syntax=docker/dockerfile:1
ARG NODE_IMAGE=registry.example.internal/base/node:22-alpine

FROM ${NODE_IMAGE} AS base
RUN apk add --no-cache libc6-compat
RUN npm install -g pnpm@10.34.5
ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH

# ---- builder: prune the monorepo to the server and its workspace deps ----
FROM base AS builder
WORKDIR /app
RUN pnpm add -g turbo@2.5.4

ARG NPM_REGISTRY=https://registry.example.internal/npm/
ENV NPM_CONFIG_REGISTRY=$NPM_REGISTRY

COPY . .

# Only what @example/server needs ends up in /app/out:
#   out/json            package.json files
#   out/full            sources
#   out/pnpm-lock.yaml  the lockfile, pruned to those packages
RUN turbo prune @example/server --docker

# ---- installer: install from the pruned lockfile, then build ----
FROM base AS installer
WORKDIR /app
COPY --from=builder /app/out/json/ .
COPY --from=builder /app/out/pnpm-lock.yaml ./pnpm-lock.yaml
RUN pnpm install --frozen-lockfile

COPY --from=builder /app/out/full/ .
RUN pnpm turbo run build --filter=@example/server

# ---- runner ----
FROM registry.example.internal/base/node:22-alpine AS runner
WORKDIR /app
RUN addgroup --system --gid 1001 app && adduser --system --uid 1001 app
USER app
COPY --from=installer --chown=app:app /app .
EXPOSE 3000
CMD ["node", "apps/server/dist/index.js"]
