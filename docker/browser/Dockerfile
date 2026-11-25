# One browser only. No application code or credentials belong in this image.
FROM node:22-bookworm-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
WORKDIR /opt/evo-browser
RUN npm init --yes \
    && npm install --save-exact playwright@1.64.1 \
    && npx playwright install --with-deps --only-shell chromium \
    && chmod -R a+rX /opt/evo-browser /opt/ms-playwright \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/* /root/.cache
