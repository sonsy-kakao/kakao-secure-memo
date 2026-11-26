# ---- 1단계: 의존성 설치 (빌드 산출물만 다음 단계로 복사) ----
FROM python:3.12-alpine AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- 2단계: 런타임 (경량 alpine + 비root 사용자) ----
FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN addgroup -S app && adduser -S -G app -H -s /sbin/nologin app
COPY --from=builder /install /usr/local
WORKDIR /srv
COPY --chown=app:app . .
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8000/healthz || exit 1
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
