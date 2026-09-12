# Runs the web dashboard. Config, device inventories and secrets are
# never baked into the image - mount your own config/ directory at
# runtime (see docker-compose.yml). See docs/deployment.md.
FROM python:3.12-slim

WORKDIR /app

# Installing the package itself (not just requirements.txt) is what sets
# up the `localhome` entry point and bundles templates/static correctly -
# see pyproject.toml's [tool.setuptools.package-data].
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

EXPOSE 5000
ENV LOCALHOME_CONFIG=/app/config/config.yaml
VOLUME ["/app/config"]

CMD ["localhome"]
