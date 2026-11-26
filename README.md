# 🔐 클라우드 엔지니어 보안 메모장 — 3-Tier 컨테이너 보안 웹 서비스

카카오클라우드 6기 11월 실기평가 과제입니다. 메모리에 데이터를 저장하던 레거시 앱을 **Nginx → Flask → MariaDB** 3-Tier 구조로 고도화했습니다. 토큰 기반 인증, 웹 보안 방어, 네트워크 격리, CI/CD 무중단 배포를 함께 적용했습니다.

## 1. 아키텍처

```
                 ┌──────────────── Docker Host ────────────────────────────────┐
  사용자 ──80──▶ │  web (Nginx)  ──frontend──▶  app (Flask/Gunicorn)            │
  (HTTP)         │   :8080 비root                   :8000 비root               │
                 │                                     │                        │
                 │                          backend (internal: true)            │
                 │                                     ▼                        │
                 │                              db (MariaDB 11.4)               │
                 │                              volume: db_data                 │
                 └──────────────────────────────────────────────────────────────┘
  GitHub push(main) ─▶ Actions: 빌드 → Docker Hub push → SSH → deploy.sh (롤링 배포)
```

| 구분 | 네트워크 | 호스트 포트 | 비고 |
|---|---|---|---|
| web | frontend | **80 → 8080** | 외부에 노출되는 유일한 진입점 |
| app | frontend + backend | 없음 | Nginx를 통해서만 접근 |
| db | backend(**internal**) | **없음** | app과만 통신, 호스트·web·인터넷에서 도달 불가 |

## 2. 요구사항 대응표

| 요구사항 | 구현 |
|---|---|
| Stateless 토큰 인증 `POST /api/login` | JWT(HS256, 30분). `alg` 고정으로 `alg=none` 공격 차단, `jti` 기반 로그아웃 시 토큰 폐기 |
| 테스트 계정 자동 생성 | Gunicorn 마스터의 `on_starting`에서 1회 실행 → 스키마 생성 + `tester / test1234!` 시드(멱등). 워커 간 경쟁 조건 없음 |
| 이름 / GitHub ID 출력 | `users.display_name`, `users.github_id`를 `GET /api/me`로 **DB에서 조회**해 상단에 출력 |
| JS 접근 불가 보안 쿠키 | `access_token`: `HttpOnly; SameSite=Strict; Path=/` (HTTPS일 때 `Secure` 자동 부여) |
| XSS 방어 | 서버는 JSON만 응답, 프론트는 `textContent`로만 렌더링(`innerHTML` 미사용). CSP `script-src 'self'`, `nosniff`, 입력 길이·제어문자 검증 |
| CSRF 방어 | ① `SameSite=Strict` ② Origin/Referer 검증 ③ JWT에 묶인 CSRF 토큰을 `X-CSRF-Token` 헤더로 요구(Signed Double-Submit) |
| Replay 방어 | 상태 변경 요청마다 서버가 발급한 **1회용 Nonce**(`GET /api/nonce`, TTL 120초)를 `X-Request-Nonce`로 요구. 사용 즉시 DB에서 `DELETE`로 원자적으로 소멸 → 같은 요청을 재전송하면 `409` |
| SQL Injection | 모든 쿼리에 파라미터 바인딩 사용 |
| 무차별 대입 방어 | Nginx `limit_req`: 로그인 분당 5회(+burst 5), API 초당 20회 |
| 데이터 영속성 | MariaDB + named volume `db_data` |
| 경량 이미지 | `python:3.12-alpine` 멀티스테이지, `nginx:1.27-alpine`, 비root 실행, `read_only`, `cap_drop: ALL`, `no-new-privileges` |
| 3-Tier 격리 | `backend` 네트워크 `internal: true`, db `ports` 없음, web은 backend에 미연결 |
| 민감 정보 외부 주입 | 모든 값은 `.env`(로컬) / GitHub Secret `PROD_ENV_FILE`(운영)로 주입. 누락 시 compose가 기동 거부(`${VAR:?}`) |
| 기동 순서 | db `healthcheck.sh --innodb_initialized` → app `depends_on: service_healthy` + 앱 내 재시도 루프 → web은 app이 healthy일 때 기동 |
| CI/CD | `main` push → 이미지 2종 빌드 → Docker Hub(`latest`, `커밋SHA`) → SSH 배포 |
| 배포 충돌 방지 | `container_name` 미고정, 워크플로 `concurrency` 직렬화 + `flock`, **롤링 교체**(새 컨테이너 healthy 확인 후 기존 컨테이너 graceful 종료), 실패 시 새 컨테이너 제거 |
| 자격 증명 암호화 | Docker Hub **Access Token**, SSH 키, `.env` 전체를 GitHub Encrypted Secrets로 관리 |

## 3. 로컬 실행

```bash
cp .env.example .env && chmod 600 .env   # 값 수정 (OWNER_NAME, OWNER_GITHUB_ID 등)
docker compose up -d --build
docker compose ps                         # 3개 모두 healthy 확인
# 브라우저: http://localhost  → tester / test1234!
```

## 4. 운영 배포 설정

### 4-1. 운영 서버 최초 준비 (1회)
```bash
# Ubuntu 기준
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
mkdir -p ~/secure-memo
# 방화벽/보안그룹: 22(SSH, 가능하면 IP 제한), 80만 인바운드 허용. 3306은 열지 않음
```

### 4-2. GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | 값 |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub ID |
| `DOCKERHUB_TOKEN` | Docker Hub → Account settings → Personal access tokens (Read & Write) |
| `SSH_HOST` | 운영 서버 공인 IP |
| `SSH_USER` | 예: `ubuntu` |
| `SSH_PRIVATE_KEY` | 서버 접속용 개인키 전체 (`-----BEGIN ... END-----` 포함) |
| `SSH_PORT` | (선택) 기본 22 |
| `PROD_ENV_FILE` | `.env.example`을 채운 **내용 전체** (여러 줄 그대로 붙여넣기) |

> Docker Hub 저장소 `secure-memo-app`, `secure-memo-web`은 첫 push 때 자동으로 생성됩니다. **Private**으로 바꾸면 서버에서도 `docker login`이 필요합니다.

### 4-3. 배포
`main` 브랜치에 push하면 Actions가 자동으로 실행됩니다. 수동 실행은 Actions 탭 → *Run workflow*에서 할 수 있습니다.

## 5. 검증 방법 (제출 캡처용)

**① 본인 이름/ID와 보안 쿠키**: `http://<서버IP>` 로그인 → F12 → Application → Cookies에서 `access_token`의 **HttpOnly ✓**, **SameSite Strict**를 캡처합니다. Console에서 `document.cookie`를 실행하면 `access_token`이 보이지 않는 것도 함께 캡처합니다.

**② DB 직접 접근 차단**
```bash
# (a) 외부 PC에서 → 실패해야 정상
nc -zv -w 3 <서버IP> 3306              # Connection refused / timed out

# (b) 서버 호스트에서 → 포트가 바인딩되어 있지 않음
ss -lntp | grep 3306 || echo "3306 미노출"
docker compose port db 3306            # 출력 없음

# (c) web(Nginx) 컨테이너 내부에서 → 이름 해석부터 실패
docker compose exec web nc -zv -w 3 db 3306     # nc: bad address 'db'
docker compose exec web wget -T 3 -qO- http://db:3306 ; echo "exit=$?"

# (d) 비교: app 컨테이너에서는 접근 가능 (유일한 허용 경로)
docker compose exec app python -c "import socket;socket.create_connection(('db',3306),3);print('app→db OK')"

# (e) 네트워크 구성 증빙
docker network inspect secure-memo_backend --format '{{.Internal}} {{range .Containers}}{{.Name}} {{end}}'
```

**③ 보안 로직 (curl)**
```bash
B=http://<서버IP>; H="Origin: $B"
nonce(){ curl -s $B/api/nonce | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])'; }
N=$(nonce)
curl -si -c jar -H "$H" -H "X-Request-Nonce: $N" -H 'Content-Type: application/json' \
     -d '{"username":"tester","password":"test1234!"}' $B/api/login | grep -i set-cookie
# 같은 Nonce로 재전송 → 409 (Replay 차단)
curl -s -H "$H" -H "X-Request-Nonce: $N" -H 'Content-Type: application/json' \
     -d '{"username":"tester","password":"test1234!"}' $B/api/login
# CSRF 토큰 없이 작성 → 403
curl -s -b jar -H "$H" -H "X-Request-Nonce: $(nonce)" -H 'Content-Type: application/json' \
     -d '{"title":"t","content":"c"}' $B/api/memos
```

**④ 영속성**: 메모 작성 → `docker compose down && docker compose up -d`를 실행한 뒤에도 메모가 남아 있는지 확인합니다. (`down -v`는 볼륨까지 삭제하므로 사용하지 마세요.)

## 6. API

| Method | Path | 인증 | 헤더 |
|---|---|---|---|
| GET | `/api/nonce` | - | - |
| POST | `/api/login` | - | `X-Request-Nonce` |
| POST | `/api/logout` | ✓ | `X-CSRF-Token`, `X-Request-Nonce` |
| GET | `/api/me` | ✓ | - |
| GET | `/api/memos` | ✓ | - |
| POST | `/api/memos` | ✓ | `X-CSRF-Token`, `X-Request-Nonce` |

## 7. 디렉터리 구조
```
.
├── app/                 # WAS (Flask + Gunicorn)
│   ├── app.py
│   ├── gunicorn.conf.py
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── templates/       # login.html, index.html
│   └── static/          # api.js, login.js, main.js, style.css
├── nginx/               # Reverse Proxy
│   ├── nginx.conf
│   └── Dockerfile
├── deploy/deploy.sh     # 운영 서버 롤링 배포 스크립트
├── .github/workflows/deploy.yml
├── docker-compose.yml
├── .env.example         # 템플릿만 커밋 (.env는 .gitignore)
└── docs/AI_LOG.md       # AI 프롬프트 엔지니어링 & 트러블슈팅 일지
```

## 8. 한계 및 개선 방향
- 현재 HTTP(80)만 사용하므로 쿠키 `Secure` 플래그가 붙지 않습니다(브라우저가 HTTP에서는 Secure 쿠키를 저장하지 않음). 도메인을 연결하고 Let's Encrypt로 443을 적용하면 `COOKIE_SECURE=auto`에 따라 자동으로 붙고, HSTS도 추가할 수 있습니다.
- web 이미지가 바뀌는 배포에서는 Nginx 재생성으로 1~2초 끊김이 생길 수 있습니다. 완전한 무중단이 필요하면 LB 뒤에 2대 이상을 두거나 blue/green 방식을 적용합니다.
