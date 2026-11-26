# AI 프롬프트 엔지니어링 & 트러블슈팅 일지

> ✏️ **작성 안내**: 이 문서는 초안입니다. Part B의 에러는 실제 구축·검증 과정에서 **실제로 발생한 것**입니다. 본인이 직접 겪은 에러와 사용한 프롬프트로 내용을 보강·수정하고, 각 항목에 터미널 캡처를 첨부하세요.

---

## Part A. 프롬프트 고도화 과정 (3건)

### A-1. 요구사항 분할: "보안 메모장 만들어줘" → 계층별 분리 설계
- **1차 프롬프트**: "Flask로 로그인 되는 메모장 만들어줘"
- **문제점**: 세션(서버 메모리) 기반 인증, SQLite 사용, 보안 로직 없음 → 평가 요구사항과 불일치
- **고도화 프롬프트**
  ```
  역할: DevOps/클라우드 보안 엔지니어.
  다음을 "설계 → 구현" 순서로, 계층별로 나눠서 답해줘.
  1) WAS: Flask, 인증은 서버 세션 없이 JWT. 토큰은 HttpOnly·SameSite=Strict 쿠키로만 발급.
  2) DB: MariaDB, PyMySQL 파라미터 바인딩만 사용(ORM 미사용), 스키마는 앱 기동 시 멱등 생성.
  3) 시드: tester / test1234! 계정을 기동 시 자동 생성하되 비밀번호는 환경변수로 주입.
  제약: 소스 코드에 비밀값 하드코딩 금지, 환경변수 누락 시 즉시 기동 실패.
  출력: 파일 단위 코드 + 각 보안 결정의 근거 1줄.
  ```
- **결과**: 인증, DB, 시드가 분리된 구조와 `required_env()`로 빠르게 실패(fail-fast)하는 설정 로직을 얻음

### A-2. 제약 조건 부여: CSRF/Replay 방어 방식 비교 후 선택
- **1차 응답**: CSRF 토큰만 제안하고, Replay는 "timestamp + 클라이언트 nonce(crypto.randomUUID)" 방식 제안
- **추가 제약**: "운영 서버는 HTTPS 없이 IP:80으로 접속한다. 브라우저 Secure Context 제약, 클라이언트 시계 오차, Gunicorn 멀티 워커(메모리 비공유) 환경을 모두 고려해서 다시 설계해줘."
- **개선 결과**
  - `crypto.randomUUID()`는 Secure Context(HTTPS/localhost)에서만 동작 → HTTP IP 접속에서는 실패하므로 **서버 발급 1회용 Nonce**로 변경
  - 워커 간 메모리가 공유되지 않으므로 Nonce 저장소를 **DB 테이블**로 옮기고 `DELETE ... rowcount==1`로 원자적으로 소비
  - CSRF 토큰을 JWT claim에 묶어(Signed Double-Submit) 쿠키 주입 공격도 무력화

### A-3. 검증 기준 명시: "배포 자동화" → "충돌 없는 롤링 배포"
- **1차 응답**: `docker compose down && docker compose up -d` → 배포 중 서비스 중단, `container_name` 고정 시 이름 충돌 발생
- **고도화 프롬프트**
  ```
  단일 서버 + docker compose 환경에서 다음 조건을 만족하는 배포 스크립트를 작성해줘.
  - 새 컨테이너가 healthy 상태가 되기 전에는 기존 컨테이너를 내리지 않을 것
  - healthy가 되지 않으면 새 컨테이너만 제거하고 기존 서비스는 유지(롤백)
  - 동시에 두 배포가 실행되지 않을 것
  - Nginx가 바뀐 컨테이너 IP를 재시작 없이 따라갈 것
  ```
- **결과**: `--scale app=2 --no-recreate` 롤링, `flock` + Actions `concurrency`, Nginx `resolver 127.0.0.11 valid=5s` + 변수 기반 `proxy_pass` 구성

---

## Part B. 트러블슈팅 (실제 발생 에러)

### B-1. 정상 로그인이 `403 허용되지 않은 출처(Origin)` 로 실패 — Nginx Host 헤더의 포트 누락
- **증상**: 8081 포트로 띄운 테스트 환경에서 정상 로그인 요청까지 전부 403
- **원인 분석 (AI와 함께)**: 브라우저의 `Origin: http://localhost:8081`과 Flask의 `request.host` 값(`localhost`)이 일치하지 않음. Nginx의 `proxy_set_header Host $host`는 **포트를 제거한 호스트명**을 전달함
- **해결**: `proxy_set_header Host $http_host;`로 변경해 클라이언트가 보낸 Host(포트 포함)를 그대로 전달
- **교훈**: 80 포트 운영 환경에서는 드러나지 않는 버그라서, 비표준 포트 환경에서도 테스트해야 함

### B-2. 메모 작성일이 `%Y-%m-%d %H:%i:%s` 문자열 그대로 출력
- **증상**: `GET /api/memos` 응답의 `created_at`이 날짜가 아니라 포맷 문자열로 나옴
- **원인**: PyMySQL은 **바인딩 파라미터가 있을 때만** `%` 포매팅을 수행함. 파라미터 없는 쿼리에 `%%Y`로 이스케이프했더니 `%%`가 그대로 MariaDB에 전달되어 리터럴 `%`로 해석됨
- **해결**: 파라미터 없는 쿼리에서는 `%Y-%m-%d`를 이스케이프 없이 사용하고, 코드에 주석으로 남김

### B-3. API 응답 한글이 `인증...` 으로 출력
- **원인**: Flask 3.x에서는 `app.config["JSON_AS_ASCII"]` 설정이 **무시됨**(2.3부터 deprecated)
- **해결**: `app.json.ensure_ascii = False`로 변경

### B-4. (참고) 로컬 Nginx 재시작이 반영되지 않음
- **증상**: 설정을 고쳤는데도 같은 에러가 반복됨. `nginx -s stop` 실행 시 `open() "/run/nginx.pid" failed`
- **원인**: 비root 실행을 위해 `pid /tmp/nginx.pid`로 바꿨는데, `-s stop`이 기본 pid 경로를 참조해 **기존 프로세스가 계속 떠 있었음**(새 프로세스는 `bind() ... Address already in use`로 실패)
- **해결**: `nginx -s stop -c <설정파일>`로 같은 설정을 지정하거나 기존 프로세스를 종료한 뒤 재기동. 컨테이너 환경에서는 `docker compose up -d --force-recreate web`으로 교체

### (본인 작성) B-5. 컨테이너 기동 순서 / 권한 / 네트워크 에러
> 예: 실제 Docker 환경에서 `app`이 `db` 초기화 전에 접속을 시도해 `OperationalError (2003)`가 발생 → `healthcheck.sh --innodb_initialized` + `depends_on: condition: service_healthy` + 앱 재시도 루프로 해결.
> 직접 겪은 로그와 캡처로 교체하세요.
