#!/usr/bin/env bash
# 운영 서버 무중단(롤링) 배포 스크립트
#   1) 새 이미지 pull
#   2) 새 app 컨테이너를 기존 것과 "나란히" 1개 더 띄움 (scale 2)
#   3) 새 컨테이너가 healthy 가 되면 기존 컨테이너를 graceful 종료
#   4) web(Nginx)은 Docker DNS를 5초마다 재조회하므로 새 컨테이너로 자동 전환
# 동시 실행 방지: flock (워크플로우 concurrency 와 이중 방어)
set -euo pipefail

cd "$(dirname "$0")"
exec 9>/tmp/secure-memo-deploy.lock
flock -n 9 || { echo "다른 배포가 진행 중입니다."; exit 1; }

[ -f .env ] || { echo ".env 파일이 없습니다."; exit 1; }
chmod 600 .env

COMPOSE="docker compose -f docker-compose.yml"

echo "▶ 이미지 pull (tag=${IMAGE_TAG:-latest})"
$COMPOSE pull app web

echo "▶ DB 기동 확인"
$COMPOSE up -d db

OLD_IDS=$($COMPOSE ps -q app || true)

if [ -z "$OLD_IDS" ]; then
  echo "▶ 최초 배포: 전체 기동"
  $COMPOSE up -d --remove-orphans
else
  echo "▶ 롤링 배포: 새 app 컨테이너 추가"
  $COMPOSE up -d --no-deps --no-recreate --scale app=2 app

  NEW_ID=""
  for id in $($COMPOSE ps -q app); do
    echo "$OLD_IDS" | grep -q "$id" || NEW_ID="$id"
  done
  [ -n "$NEW_ID" ] || { echo "새 컨테이너를 찾지 못했습니다."; exit 1; }

  echo "▶ 새 컨테이너(${NEW_ID:0:12}) 헬스체크 대기"
  for i in $(seq 1 30); do
    STATUS=$(docker inspect -f '{{.State.Health.Status}}' "$NEW_ID")
    [ "$STATUS" = "healthy" ] && break
    if [ "$i" -eq 30 ]; then
      echo "✖ 새 컨테이너가 healthy 상태가 되지 않음 → 롤백"
      docker logs --tail 50 "$NEW_ID" || true
      docker rm -f "$NEW_ID"
      exit 1
    fi
    sleep 3
  done

  echo "▶ 기존 컨테이너 graceful 종료 (DNS TTL 대기 후)"
  sleep 6
  for id in $OLD_IDS; do
    docker stop -t 25 "$id" >/dev/null && docker rm "$id" >/dev/null
  done
  $COMPOSE up -d --no-deps --no-recreate --scale app=1 app

  echo "▶ web 갱신 (이미지가 바뀐 경우에만 재생성)"
  $COMPOSE up -d --no-deps web
fi

echo "▶ 미사용 이미지 정리"
docker image prune -f >/dev/null

$COMPOSE ps
echo "✔ 배포 완료"
