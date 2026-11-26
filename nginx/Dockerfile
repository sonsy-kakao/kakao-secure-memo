FROM nginx:1.27-alpine
# 기본 설정 제거 후 하드닝된 설정만 사용, 비root(nginx 사용자)로 실행
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/nginx.conf
USER nginx
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD wget -qO- http://127.0.0.1:8080/nginx-health || exit 1
