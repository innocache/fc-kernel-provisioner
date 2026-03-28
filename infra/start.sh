#!/bin/sh
set -e

BACKENDS="${BACKENDS:-localhost:8000}"

mkdir -p /data/caddy /data/redis

echo "Starting Redis..."
redis-server \
  --port 6379 \
  --bind 0.0.0.0 \
  --save "" \
  --appendonly no \
  --maxmemory 64mb \
  --maxmemory-policy allkeys-lru \
  --daemonize yes \
  --logfile /data/redis/redis.log

echo "Waiting for Redis..."
until redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.2; done
echo "Redis ready."

echo "Backends: $BACKENDS"
export BACKENDS

echo "Starting Caddy..."
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
