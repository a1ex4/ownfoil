#!/bin/bash

# Get root_dir from env, defaults to /games
root_dir="${ROOT_DIR:-/games}"

gid=${PGID:-1000}
uid=${PUID:-1000}
# Setup non root user
if [ $(getent group $gid) ]
then
    gt_group=$(getent group $gid | cut -d: -f1)
    echo "Group ${gt_group} with GID ${gid} already exists, skip creation"
else
    echo "Creating group app with GID ${gid}"
    addgroup -g ${gid} -S app
    gt_group=$(getent group $gid | cut -d: -f1)
fi

if [ $(getent passwd $uid) ]
then
    echo "User ${gt_user} with UID ${uid} already exists, skip creation"
else
    echo "Creating user app with UID ${uid}"
    adduser -u ${uid} -S app -G ${gt_group}
    gt_user=$(getent passwd $uid | cut -d: -f1)
fi

chown -R ${uid}:${gid} /app
chown -R ${uid}:${gid} $root_dir/games

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
sudo -u $gt_user python /app/app.py $root_dir/shop_config.toml
