#!/bin/sh
set -u

if ! systemctl is-active --quiet nginx; then
    logger -t nginx-watchdog "nginx inactive, starting"
    systemctl start nginx
    exit 0
fi

code=$(/usr/bin/curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -H 'Host: server.indemw.tech' http://127.0.0.1/ 2>/dev/null || echo 000)

if [ "$code" = "000" ]; then
    logger -t nginx-watchdog "nginx not answering (000), restarting"
    systemctl restart nginx
fi
