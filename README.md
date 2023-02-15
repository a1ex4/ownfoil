# Ownfoil
[![Docker Image Version (latest semver)](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)](https://github.com/a1ex4/ownfoil/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil)](https://hub.docker.com/r/a1ex4/ownfoil)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil)](https://hub.docker.com/r/a1ex4/ownfoil/tags)
![Tinfoil Version](https://img.shields.io/badge/Tinfoil-v15.00-green)
![Awoo Version](https://img.shields.io/badge/Awoo-v1.3.4-red)

Ownfoil is a simple webserver aimed at running your own Tinfoil/Awoo shop from your local library, with full shop customisation and authentication. It is designed to periodically scan your library (default every 5 minutes), generate Tinfoil index file and serve it all over HTTP/S. This makes it easy to manage your library and have your personal collection available at any time.

Why this project? I wanted a lightweight, dead simple, no dependancy and private personal Shop, without having to rely on other proprietary services (Google, 1fichier...) and having to maintain their implementation.

# Table of Contents
- [Usage](#usage)
- [Configuration](#configuration)
- [Shop Customization](#shop-customization)
- [Setup Authentication](#setup-authentication)
- [Changelog](#changelog)
- [Similar Projects](#similar-projects)

# Usage
Ownfoil is shipped as a Docker container for simplicity and compatibility. You first need to [install Docker](https://docs.docker.com/get-docker/). Come back when you have a working installation!

Then, there are two ways to start the container, with `docker run` or `docker-compose`.

Use the `PUID` and `PGID` environment variables to make sure the app will have write access to your game directory.

## Docker run

Running this command will start the shop on port `8000` with the library in `/your/game/directory` :

    docker run -d -p 8000:80 -v /your/game/directory:/games --name ownfoil a1ex4/ownfoil

The shop is now accessible with your computer/server IP and port, i.e. `http://localhost:8000` from the same computer or `http://192.168.1.100:8000` from a device in your network.

## Docker compose
Create a file named `docker-compose.yml` with the following content:
```
version: "3"

services:
  ownfoil:
    container_name: ownfoil
    image: a1ex4/ownfoil
    environment:
      # For write permission in /games directory
      - PUID=1000
      - PGID=1000
      # Setup auth
      - USERNAME=a1ex
      - PASSWORD=pass
      # - ROOT_DIR=/games
    volumes:
      - /storage/media/games/switch:/games
    ports:
      - "8000:80"
```

You can then create and start the container with the command (executed in the same directory as the docker-compose file):

    docker-compose up -d

This is usefull if you don't want to remember the `docker run` command and have a persistent and reproductible container configuration.

## Tinfoil setup
In Tinfoil, add a shop with the following settings:
 - Protocol: `http`
 - Host: server/computer IP, i.e. `192.168.1.100`
 - Port: host port of the container, i.e. `8000`
 - Username: same as `USERNAME` env if authentication is enabled
 - Password: same as `PASSWORD` env if authentication is enabled

# Configuration
On the first run of Ownfoil, a `shop_config.toml` file will be created in your games directory - use this file to configure different settings, like the scan interval.

All settings are described in the comments of the [default configuration file](./app/shop_config.toml).

Some settings can be overridden by using environment variables in the container, [see here](./app/app.py) for the list.

# Shop Customization
All [Tinfoil shop index keys](https://blawar.github.io/tinfoil/custom_index/) are configurable - at the first run of Ownfoil, a `shop_template.toml` will be created in your games directory, just fill in the keys to customize your shop.

# Setup Authentication
To enable shop authentication, simply define and set the `USERNAME` and `PASSWORD` environment variables inside the container. See the [docker-compose](#docker-compose) example.

# Changelog

## 1.1.0
 - Container now support PUID/PGID to have the same permissions as the host user
 - Rewrote Authentication setup to simplify it
 - Switch to TOML for shop configuration and customization

## 1.0.1
- Fix shop.tfl generation: use path relative to the index file (fixes #1)
## 1.0.0

- Initial release

# Similar Projects
If you want to create your personal NSP Shop then check out these other similar projects:
- [eXhumer/pyTinGen](https://github.com/eXhumer/pyTinGen)
- [JackInTheShop/FT-SCEP](https://github.com/JackInTheShop/FT-SCEP)
- [gianemi2/tinson-node](https://github.com/gianemi2/tinson-node)
- [BigBrainAFK/tinfoil_gdrive_generator](https://github.com/BigBrainAFK/tinfoil_gdrive_generator)
- [ibnux/php-tinfoil-server](https://github.com/ibnux/php-tinfoil-server)
- [ramdock/nut-server](https://github.com/ramdock/nut-server)
- [Myster-Tee/TinfoilWebServer](https://github.com/Myster-Tee/TinfoilWebServer)
- [DevYukine/rustfoil](https://github.com/DevYukine/rustfoil)
- [Orygin/gofoil](https://github.com/Orygin/gofoil)