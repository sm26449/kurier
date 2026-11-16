FROM python:3.11-slim AS base

# Unprivileged runtime user: the app writes nothing to disk.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin kurier

WORKDIR /app

COPY requirements.txt .
# Current pip for the install; setuptools/wheel are not needed at runtime.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y setuptools wheel


# Test runner with the dev dependencies baked in. The checkout is mounted
# at /app, so code changes need no rebuild:
#   docker build --target test -t kurier-test .
#   docker run --rm -v "$PWD":/app kurier-test [pytest -q tests/test_x.py -k name]
# Nothing is written into the mounted checkout (no .pyc, no pytest cache).
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["pytest", "-q", "-p", "no:cacheprovider"]


# Runtime image. Keep this stage last: `docker build .`, compose,
# scripts/deploy.sh and the release workflow all build the last stage.
FROM base AS runtime

COPY src/ src/

USER kurier
EXPOSE 5080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5080", "--log-level", "info"]
