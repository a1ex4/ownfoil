# Ownfoil
[![Docker Image Version (latest semver)](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)](https://github.com/a1ex4/ownfoil/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil)](https://hub.docker.com/r/a1ex4/ownfoil)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil)](https://hub.docker.com/r/a1ex4/ownfoil/tags)
[![Tinfoil Version](https://img.shields.io/badge/Tinfoil-v15.00-green)](https://tinfoil.io)
[![Awoo Version](https://img.shields.io/badge/Awoo-v1.3.4-red)](https://github.com/Huntereb/Awoo-Installer)

Ownfoil is a simple webserver aimed at running your own Tinfoil/Awoo shop from your local library, with full shop customisation and authentication. It is designed to periodically scan your library (default every 5 minutes), generate Tinfoil index file and serve it all over HTTP/S. This makes it easy to manage your library and have your personal collection available at any time.

Ownfoil can also be used to backup saves from multiple Switch devices, and make them available in your shop so you can use Tinfoil to reinstall them.

Why this project? I wanted a lightweight, dead simple, no dependancy and private personal Shop, without having to rely on other proprietary services (Google, 1fichier...) and having to maintain their implementation.

# Table of Contents
- [Usage](#usage)
- [Configuration](#configuration)
- [Shop Customization](#shop-customization)
- [Setup Authentication](#setup-authentication)
- [Saves Manager](#saves-manager)
- [Roadmap](#roadmap)
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
      # - SAVE_ENABLED=true
    volumes:
      - /your/game/directory:/games
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

Some settings can be overridden by using environment variables in the container, [see here](./app/utils.py#L13-L19) for the list.

# Shop Customization
All [Tinfoil shop index keys](https://blawar.github.io/tinfoil/custom_index/) are configurable - at the first run of Ownfoil, a `shop_template.toml` will be created in your games directory, just fill in the keys to customize your shop.

# Setup Authentication
To enable shop authentication, simply define and set the `USERNAME` and `PASSWORD` environment variables inside the container. See the [docker-compose](#docker-compose) example.

# Saves Manager
Ownfoil can be configured to backup saves from multiple Switch device and make them available in your shop, so that you can install them with Tinfoil. It uses FTP to periodically retrieve the saves.

Follow the guide below to enable an FTP server on your Switch and configure Ownfoil.

## Setup sys-ftpd on the Switch
 * Install [sys-ftpd](https://github.com/cathery/sys-ftpd) - available as `sys ftpd light` in the Homebrew Menu
 * Install [ovl-sysmodule](https://github.com/WerWolv/ovl-sysmodules) from Homebrew Menu - optional but recommended

Follow the [sys-ftpd](https://github.com/cathery/sys-ftpd#how-to-use) configuration to set up the user, password and port used for the FTP connection. Note these for Ownfoil configuration, as well as the IP of your Switch. If you installed ovl-sysmodule you can toggle on/off the FTP server using the Tesla overlay.

It is recommended to test the FTP connection at least once with a regular FTP Client to make sure everything is working as expected on the switch.

## Extract saves on the Switch
Using JKSV or Tinfoil (or any other saves manager), periodically extract your saves so that they can be retrieved by Ownfoil.

Note the folders where the saves are extracted. If you didn't change these settings, the default paths are:
 * Tinfoil: `/switch/tinfoil/saves/common`
 * JKSV: `/JKSV`

## Ownfoil configuration
In your shop [configuration file](#configuration), the save manager settings available are:

```
[saves]
# Enable or disable automatic saves backup
enabled = true

# Interval to retrieve saves, in minutes.
interval = 60
```
Make sure these settings are present if you are updating from a version < `1.2.0`.

See the default [shop default configuration file](./app/shop_config.toml) for a working configuration.

Then multiple switch can be configured, with the FTP connection details and saves directories to retrieve:
```
# Switches configuration for save retrieval.
# If user and pass are not specified, use anonymous connection
# Alex's Switch
[[saves.switches]]
host = "192.168.1.200"
port = "5000"
# user = "username"
# pass = "password"
folders = [
    {local = "Saves/Tinfoil", remote = "/switch/tinfoil/saves/common"},
    {local = "Saves/JKSV", remote = "/JKSV"}
]
```
The directories will be saved under your `games` directory so that the saves can be indexed by Ownfoil and made available in Tinfoil.

In the example above the Tinfoil saves will be saved under `./Saves/Tinfoil`

# Roadmap
Planned feature, in no particular order.
 - [x] Multi arch Docker image: currently supported platforms: `linux/amd64`, `linux/arm64`, `linux/arm/v7`, `linux/arm/v6`
 - [ ] Multiple user authentication
 - [ ] Support emulator Roms
 - [ ] Automatic nsp/xci -> nsz conversion
 - [ ] Web UI
   - list of available games/saves
   - list of available updates/DLC not present on the shop, based on currently present games.
   - operation on files
 - [ ] Use a Python webserver framework instead of nginx
   - ditch nginx
   - dynamically set server config like auth, port
 - [ ] Integrate torrent indexer Jackett to download updates automatically

# Changelog

## 1.2.0
 - Add Saves manager to automatically backup and serve saves
 - Setup base scheduler to periodically run jobs

## 1.1.1
 - Fixes typo in run.sh script, fixes #5

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