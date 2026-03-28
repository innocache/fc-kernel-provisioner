FROM alpine:3.21

RUN apk add --no-cache caddy redis

COPY Caddyfile /etc/caddy/Caddyfile
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080 6379

CMD ["/start.sh"]
