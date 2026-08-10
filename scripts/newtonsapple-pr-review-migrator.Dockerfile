FROM node:22.14-alpine AS build

WORKDIR /workspace

COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/web/package.json apps/web/package.json
COPY packages/db/package.json packages/db/package.json
COPY packages/shared/package.json packages/shared/package.json

RUN npm ci --ignore-scripts --workspace=@newtonsapple/db --include-workspace-root=false

COPY packages/shared packages/shared
COPY packages/db packages/db

RUN --network=none npm run build --workspace=@newtonsapple/shared --workspace=@newtonsapple/db

FROM node:22.14-alpine AS runtime

WORKDIR /workspace

COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/web/package.json apps/web/package.json
COPY packages/db/package.json packages/db/package.json
COPY packages/shared/package.json packages/shared/package.json

RUN npm ci --ignore-scripts --omit=dev --workspace=@newtonsapple/db --include-workspace-root=false

COPY --from=build /workspace/packages/db/dist packages/db/dist
COPY --from=build /workspace/packages/shared/dist packages/shared/dist
COPY packages/db/migrations packages/db/migrations
COPY packages/db/seeds packages/db/seeds
COPY packages/db/changes packages/db/changes

CMD ["node", "packages/db/dist/cli/bootstrap.js"]
