# Ownfoil
![Docker Image Version (latest semver)](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)
![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil)
![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil)
![Tinfoil Version](https://img.shields.io/badge/Tinfoil-v12.00-green)
![Awoo Version](https://img.shields.io/badge/Awoo-v1.3.4-red)

Ownfoil is a simple webserver aimed at running your own Tinfoil/Awoo shop from your local library, with full shop customisation and authentication. It is designed to periodically scan your library (default every 5 minutes), generate Tinfoil index file and serve it all over HTTP/S. This makes it easy to manage your library and have your personal collection available at any time.

Why this project ? I wanted a lightweight, dead simple, no dependancy and private personal Shop, without having to rely on other proprietary services (Google, 1fichier...) and having to maintain their implementation.

# Table of Contents
- [Usage](#usage)
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
      # For write permission of /games directory
      - PUID=1000
      - PGID=1000
      # Uncomment to setup basic auth
      # - USERNAME=a1ex
      # - PASSWORD=
    volumes:
      - /storage/media/games/switch:/games
    ports:
      - "8000:80"
```

You can then create and start the container with the command (executed in the same directory as the docker-compose file):

    docker-compose up -d

This is usefull if you don't want to remember the `docker run` command and have a persistent and reproductible container configuration.

# Shop Customization
All [Tinfoil shop index keys](https://blawar.github.io/tinfoil/custom_index/) are configurable - at the first run of Ownfoil, a `shop_template.jsonc` will be created in your games directory, just fill in the keys to customize your shop.

Be careful with the formatting of the file, especially of commas separating the keys.

# Setup Authentication
If the `USERNAME` and `PASSWORD` environment variables are defined inside the docker container, then basic authentication will be automatically setup.

# Changelog

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