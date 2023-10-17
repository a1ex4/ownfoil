#!/bin/bash

# Retrieve from Environment variables, or use 1000 as default
gid=${PGID:-1000}
uid=${PUID:-1000}

! getent group "${gid}" && addgroup -g "${gid}" -S ownfoil
GROUP=$(getent group "${gid}" | cut -d ":" -f 1)
! getent passwd "${uid}" && adduser -u "${uid}" -G "${GROUP}" -S ownfoil

chown -R ${uid}:${gid} /app
chown -R ${uid}:${gid} /root

echo "Starting ownfoil"

exec sudo -E -u "#${uid}" python /app/app.py
