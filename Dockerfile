FROM python:3.11-slim

# Unprivileged runtime user: the app writes nothing to disk.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin kurier

WORKDIR /app

COPY requirements.txt .
# Current pip for the install; setuptools/wheel are not needed at runtime.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y setuptools wheel

COPY src/ src/

USER kurier
EXPOSE 5080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5080", "--log-level", "info"]
