FROM node:22.11.0-slim AS build
RUN corepack enable
WORKDIR /repo
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/api-client/package.json packages/api-client/
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
COPY packages/api-client packages/api-client
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ARG NEXT_PUBLIC_AUTH_MODE=dev
RUN pnpm --filter @re/web build

FROM node:22.11.0-slim
RUN corepack enable && useradd --create-home --uid 10001 web
WORKDIR /repo
COPY --from=build /repo /repo
USER 10001
EXPOSE 3000
CMD ["pnpm", "--filter", "@re/web", "start"]
