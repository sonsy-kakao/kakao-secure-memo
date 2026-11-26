import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
threads = 2
timeout = 30
graceful_timeout = 20  # 배포 시 SIGTERM 수신 후 처리 중인 요청 마무리
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "*"  # 내부 frontend 네트워크의 Nginx만 접근 가능


def on_starting(server):
    """마스터 프로세스에서 1회만 실행 → 워커 간 스키마/시드 경쟁 조건 방지."""
    from app import init_db
    init_db(max_wait_seconds=int(os.environ.get("DB_WAIT_SECONDS", "90")))
