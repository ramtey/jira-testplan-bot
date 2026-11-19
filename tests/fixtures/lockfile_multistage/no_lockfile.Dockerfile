FROM registry.example.internal/base/node:22-alpine
WORKDIR /app
RUN npm install -g pnpm@10.34.5
COPY apps/server/package.json ./
RUN pnpm install
COPY apps/server/ .
RUN pnpm run build
CMD ["node", "dist/index.js"]
