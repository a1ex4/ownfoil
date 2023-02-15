#!/bin/bash

# Get root_dir from env, defaults to /games
root_dir="${ROOT_DIR:-/games}"

# Setup non root user
addgroup -g ${PGID:-1000} -S app && \
    adduser -u ${PUID:-1000} -S app -G app

chown -R app:app /app

# Copy the shop config and template if it does not already exists
cp -np /app/shop_config.toml $root_dir/shop_config.toml
cp -np /app/shop_template.toml $root_dir/shop_template.toml

# Setup nginx basic auth if needed
if [[ ! -z $USERNAME && ! -z $PASSWORD ]]; then
    echo "Setting up authentification for user $USERNAME."
    htpasswd -c -b /etc/nginx/.htpasswd $USERNAME $PASSWORD
    sed -i 's/# auth_basic/auth_basic/g' /etc/nginx/http.d/default.conf
else
    echo "USERNAME and PASSWORD environment variables not set, skipping authentification setup."
fi

# Start nginx and app
echo "Starting ownfoil"
nginx -g "daemon off;" &
sudo -u app python /app/ownfoil.py $root_dir/shop_config.toml
