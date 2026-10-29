ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN --mount=type=bind,source=.,target=/src,readonly \
    --mount=type=secret,id=proxy_ca \
    if [ -d /src/wheelhouse ]; then \
      pip install --no-cache-dir --no-index --find-links=/src/wheelhouse --require-hashes -r requirements.txt; \
    elif [ -f /run/secrets/proxy_ca ]; then \
      PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir --require-hashes -r requirements.txt; \
    else pip install --no-cache-dir --require-hashes -r requirements.txt; fi
COPY pulse_app /app/pulse_app
RUN chmod -R a+rX /app/pulse_app
USER 65532:65532
CMD ["python", "-m", "uvicorn", "pulse_app.app:app", "--host", "127.0.0.1", "--port", "8090"]
