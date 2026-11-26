"""
클라우드 엔지니어 보안 메모장 (WAS / Flask)

보안 설계 요약
- 인증: Stateless JWT(HS256) → HttpOnly + SameSite=Strict 쿠키로 발급 (JS 접근 불가)
- CSRF: (1) SameSite=Strict  (2) Origin/Referer 검증  (3) JWT에 바인딩된 CSRF 토큰을
        X-CSRF-Token 헤더로 요구 (Signed Double-Submit)
- Replay: 상태 변경 요청마다 서버가 발급한 1회용 Nonce(X-Request-Nonce) 필수,
          사용 즉시 DB에서 원자적으로 소멸 → 같은 요청 재전송 시 409
- XSS: 서버는 JSON만 반환(Content-Type 고정) + 프론트는 textContent로만 렌더링
       + CSP(script-src 'self') + 입력 길이/제어문자 검증
- 모든 민감 정보는 환경 변수로 외부 주입 (하드코딩 없음)
"""
import os
import re
import time
import uuid
import secrets
import hmac
import logging
from functools import wraps
from urllib.parse import urlparse

import jwt
import pymysql
from flask import Flask, jsonify, request, g, render_template, redirect, make_response
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("memo")


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"필수 환경 변수 {name} 가 설정되지 않았습니다.")
    return value


# ---------------------------------------------------------------------------
# 설정 (전부 외부 주입)
# ---------------------------------------------------------------------------
JWT_SECRET = required_env("JWT_SECRET")
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "1800"))
NONCE_TTL_SECONDS = int(os.environ.get("NONCE_TTL_SECONDS", "120"))
# auto: HTTPS(X-Forwarded-Proto=https)로 들어온 요청이면 Secure 플래그 자동 부여
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "auto").lower()

DB_CONFIG = dict(
    host=os.environ.get("DB_HOST", "db"),
    port=int(os.environ.get("DB_PORT", "3306")),
    user=required_env("DB_USER"),
    password=required_env("DB_PASSWORD"),
    database=required_env("DB_NAME"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=True,
    connect_timeout=5,
)

ACCESS_COOKIE = "access_token"
CSRF_COOKIE = "csrf_token"
MAX_TITLE = 100
MAX_CONTENT = 2000
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

app = Flask(__name__)
app.json.ensure_ascii = False  # Flask 3.x: JSON_AS_ASCII 설정은 무시됨
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
# Nginx 1단 뒤에 있으므로 X-Forwarded-For / X-Forwarded-Proto 1홉만 신뢰
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = pymysql.connect(**DB_CONFIG)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        display_name VARCHAR(50) NOT NULL,
        github_id VARCHAR(50) NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS memos (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        title VARCHAR(100) NOT NULL,
        content TEXT NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_created (created_at),
        CONSTRAINT fk_memo_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS request_nonces (
        nonce CHAR(43) PRIMARY KEY,
        expires_at INT NOT NULL,
        INDEX idx_exp (expires_at)
    ) ENGINE=InnoDB""",
    """CREATE TABLE IF NOT EXISTS revoked_tokens (
        jti CHAR(36) PRIMARY KEY,
        expires_at INT NOT NULL,
        INDEX idx_exp (expires_at)
    ) ENGINE=InnoDB""",
]


def init_db(max_wait_seconds: int = 60):
    """DB가 준비될 때까지 재시도 후 스키마 생성 + 테스트 계정 시드 (멱등)."""
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            conn = pymysql.connect(**DB_CONFIG)
            break
        except pymysql.err.OperationalError as e:
            if time.time() > deadline:
                raise
            log.warning("DB 연결 대기 중 (시도 %d): %s", attempt, e)
            time.sleep(2)

    seed_user = os.environ.get("SEED_USERNAME", "tester")
    seed_pw = required_env("SEED_PASSWORD")
    owner_name = required_env("OWNER_NAME")
    owner_github = required_env("OWNER_GITHUB_ID")
    try:
        with conn.cursor() as cur:
            for ddl in SCHEMA:
                cur.execute(ddl)
            cur.execute("SELECT id FROM users WHERE username=%s", (seed_user,))
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT IGNORE INTO users (username, password_hash, display_name, github_id) "
                    "VALUES (%s, %s, %s, %s)",
                    (seed_user, generate_password_hash(seed_pw), owner_name, owner_github),
                )
                log.info("테스트 계정 '%s' 생성 완료", seed_user)
            else:
                # 이름/GitHub ID는 .env 값으로 동기화 (비밀번호는 유지)
                cur.execute(
                    "UPDATE users SET display_name=%s, github_id=%s WHERE username=%s",
                    (owner_name, owner_github, seed_user),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 보안 헬퍼
# ---------------------------------------------------------------------------
def error(status: int, message: str):
    return jsonify({"ok": False, "error": message}), status


def cookie_secure() -> bool:
    if COOKIE_SECURE == "true":
        return True
    if COOKIE_SECURE == "false":
        return False
    return request.is_secure


def issue_token(user_id: int):
    now = int(time.time())
    csrf = secrets.token_urlsafe(32)
    payload = {
        "sub": str(user_id),
        "jti": str(uuid.uuid4()),
        "csrf": csrf,
        "iat": now,
        "nbf": now,
        "exp": now + JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256"), csrf


def decode_token():
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    try:
        claims = jwt.decode(
            token, JWT_SECRET, algorithms=["HS256"],  # alg 고정 → alg=none 공격 차단
            options={"require": ["sub", "jti", "csrf", "exp", "iat"]},
        )
    except jwt.PyJWTError:
        return None
    with get_db().cursor() as cur:
        cur.execute("SELECT 1 FROM revoked_tokens WHERE jti=%s", (claims["jti"],))
        if cur.fetchone():
            return None
    return claims


def origin_ok() -> bool:
    """Origin(없으면 Referer)의 host가 요청 Host와 같은지 확인."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True  # 헤더 미전송 클라이언트는 CSRF 토큰 검증에 의존
    return urlparse(source).netloc == request.host


def consume_nonce() -> bool:
    """1회용 Nonce 원자적 소비. 이미 사용됐거나 만료됐으면 False."""
    nonce = request.headers.get("X-Request-Nonce", "")
    if not nonce or len(nonce) > 43:
        return False
    with get_db().cursor() as cur:
        cur.execute(
            "DELETE FROM request_nonces WHERE nonce=%s AND expires_at>=%s",
            (nonce, int(time.time())),
        )
        return cur.rowcount == 1


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        claims = decode_token()
        if claims is None:
            return error(401, "인증이 필요합니다.")
        g.claims = claims
        g.user_id = int(claims["sub"])
        return fn(*args, **kwargs)
    return wrapper


def protect_state_change(require_csrf: bool = True):
    """POST 등 상태 변경 요청: Origin + CSRF 토큰 + Replay(Nonce) 방어."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not origin_ok():
                return error(403, "허용되지 않은 출처(Origin)입니다.")
            if require_csrf:
                sent = request.headers.get("X-CSRF-Token", "")
                if not sent or not hmac.compare_digest(sent, g.claims["csrf"]):
                    return error(403, "CSRF 토큰이 유효하지 않습니다.")
            if not consume_nonce():
                return error(409, "재전송(Replay)되었거나 만료된 요청입니다.")
            return fn(*args, **kwargs)
        return wrapper
    return deco


def clean_text(value, limit: int, field: str):
    if not isinstance(value, str):
        raise ValueError(f"{field} 값이 올바르지 않습니다.")
    value = CONTROL_CHARS.sub("", value).strip()
    if not value:
        raise ValueError(f"{field}을(를) 입력하세요.")
    if len(value) > limit:
        raise ValueError(f"{field}은(는) {limit}자 이하로 입력하세요.")
    return value


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# 페이지
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    if decode_token() is None:
        return redirect("/login")
    return render_template("index.html")


@app.get("/login")
def login_page():
    if decode_token() is not None:
        return redirect("/")
    return render_template("login.html")


@app.get("/healthz")
def healthz():
    try:
        with get_db().cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify({"status": "ok"})
    except Exception:  # noqa: BLE001
        return jsonify({"status": "db_unavailable"}), 503


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/nonce")
def get_nonce():
    """상태 변경 요청 직전에 받아가는 1회용 Nonce (Replay 방어용)."""
    nonce = secrets.token_urlsafe(32)
    now = int(time.time())
    with get_db().cursor() as cur:
        cur.execute("DELETE FROM request_nonces WHERE expires_at<%s", (now,))
        cur.execute(
            "INSERT INTO request_nonces (nonce, expires_at) VALUES (%s, %s)",
            (nonce, now + NONCE_TTL_SECONDS),
        )
    return jsonify({"nonce": nonce, "expires_in": NONCE_TTL_SECONDS})


@app.post("/api/login")
@protect_state_change(require_csrf=False)  # 로그인 전에는 CSRF 토큰이 없으므로 Origin + Nonce로 방어
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", ""))[:50]
    password = str(data.get("password", ""))[:128]
    with get_db().cursor() as cur:
        cur.execute("SELECT id, password_hash FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        log.warning("로그인 실패 username=%r ip=%s", username, request.remote_addr)
        return error(401, "아이디 또는 비밀번호가 올바르지 않습니다.")

    token, csrf = issue_token(user["id"])
    resp = make_response(jsonify({"ok": True}))
    secure = cookie_secure()
    resp.set_cookie(ACCESS_COOKIE, token, max_age=JWT_TTL_SECONDS, httponly=True,
                    secure=secure, samesite="Lax", path="/")
    # CSRF 토큰은 JS가 읽어 헤더로 보내야 하므로 HttpOnly 아님 (JWT에 바인딩되어 위조 불가)
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=JWT_TTL_SECONDS, httponly=False,
                    secure=secure, samesite="Strict", path="/")
    log.info("로그인 성공 user_id=%s ip=%s", user["id"], request.remote_addr)
    return resp


@app.post("/api/logout")
@login_required
@protect_state_change()
def logout():
    with get_db().cursor() as cur:
        cur.execute("INSERT IGNORE INTO revoked_tokens (jti, expires_at) VALUES (%s, %s)",
                    (g.claims["jti"], g.claims["exp"]))
        cur.execute("DELETE FROM revoked_tokens WHERE expires_at<%s", (int(time.time()),))
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(ACCESS_COOKIE, path="/", samesite="Lax")
    resp.delete_cookie(CSRF_COOKIE, path="/", samesite="Strict")
    return resp


@app.get("/api/me")
@login_required
def me():
    with get_db().cursor() as cur:
        cur.execute("SELECT username, display_name, github_id FROM users WHERE id=%s", (g.user_id,))
        user = cur.fetchone()
    if not user:
        return error(401, "사용자를 찾을 수 없습니다.")
    return jsonify({"ok": True, "user": user})


@app.get("/api/memos")
@login_required
def list_memos():
    with get_db().cursor() as cur:
        cur.execute(
            "SELECT m.id, m.title, m.content, "
            # 바인딩 파라미터가 없는 쿼리이므로 % 를 이스케이프(%%)하지 않음
            "DATE_FORMAT(m.created_at, '%Y-%m-%d %H:%i:%s') AS created_at, "
            "u.display_name AS author "
            "FROM memos m JOIN users u ON u.id = m.user_id "
            "ORDER BY m.id DESC LIMIT 100"
        )
        memos = cur.fetchall()
    return jsonify({"ok": True, "memos": memos})


@app.post("/api/memos")
@login_required
@protect_state_change()
def create_memo():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error(400, "JSON 본문이 필요합니다.")
    try:
        title = clean_text(data.get("title"), MAX_TITLE, "제목")
        content = clean_text(data.get("content"), MAX_CONTENT, "내용")
    except ValueError as e:
        return error(400, str(e))
    with get_db().cursor() as cur:
        # 파라미터 바인딩 → SQL Injection 방지
        cur.execute("INSERT INTO memos (user_id, title, content) VALUES (%s, %s, %s)",
                    (g.user_id, title, content))
        memo_id = cur.lastrowid
    return jsonify({"ok": True, "id": memo_id}), 201


@app.errorhandler(404)
def not_found(_e):
    return error(404, "Not Found")


@app.errorhandler(405)
def method_not_allowed(_e):
    return error(405, "Method Not Allowed")


@app.errorhandler(413)
def too_large(_e):
    return error(413, "요청 본문이 너무 큽니다.")


if __name__ == "__main__":  # 로컬 디버그용 (운영은 gunicorn)
    init_db()
    app.run(host="127.0.0.1", port=8000)
