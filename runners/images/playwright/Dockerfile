FROM mcr.microsoft.com/playwright:v1.49.1-noble
WORKDIR /opt/re
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund && chown -R 10001:10001 /opt/re
# Bounded browser exploration (FR-8): `node /opt/re/explore.cjs config.json out.json`
COPY explore.cjs ./
ENV NODE_PATH=/opt/re/node_modules
USER 10001
WORKDIR /work
