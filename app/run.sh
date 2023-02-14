#!/bin/bash

# Setup non root user
addgroup -g ${PGID:-1000} -S app && \
    adduser -u ${PUID:-1000} -S app -G app

chown -R app:app /app

# Setup nginx basic auth if needed
if [[ ! -z $USERNAME && ! -z $PASSWORD ]]; then
    echo "Setting up authentification for user $USERNAME."
    htpasswd -c -b /etc/nginx/.htpasswd $USERNAME $PASSWORD
    sed -i 's/# auth_basic/auth_basic/g' /etc/nginx/http.d/default.conf
else
    echo "USERNAME and PASSWORD environment variables not set, skipping authentification setup."
fi

# Copy the shop template if it does not already exists
cp -np /app/shop_template.jsonc /games/shop_template.jsonc

# Start nginx and app
echo "Starting ownfoil"
nginx -g "daemon off;" &
sudo -u app python /app/gen_shop.py /games
