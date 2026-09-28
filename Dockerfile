FROM python:3.13-slim AS builder
WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim
COPY --from=builder /install /usr/local
RUN useradd --create-home --uid 10001 appuser
USER appuser
WORKDIR /home/appuser
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=2s --start-period=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]