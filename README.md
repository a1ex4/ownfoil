# <img src="https://github.com/user-attachments/assets/3cfdf010-50c3-41ae-aa86-e31b22466686" height="28"> Ownfoil
[![Static Badge](https://img.shields.io/badge/github-repo-blue?logo=github)](https://github.com/a1ex4/ownfoil)
[![Latest Release](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)](https://github.com/a1ex4/ownfoil/releases/latest)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil?sort=date&arch=amd64)](https://hub.docker.com/r/a1ex4/ownfoil/tags)  
[![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil?)](https://hub.docker.com/r/a1ex4/ownfoil)
[![Unraid downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fca.unraid.net%2Fapi%2Fsearch%3Fquery%3Downfoil%26type%3Ddocker&query=%24.hits%5B0%5D.chartData.totalDownloadsChart.data%5B6%5D&label=unraid%20downloads&color=F15A2C)](https://preview.ca.unraid.net/apps?q=ownfoil&app=v2fayr)  
![Image archs](https://img.shields.io/badge/platforms-amd64%20%7C%20%20arm64%2Fv8%20%7C%20arm%2Fv7%20%7C%20arm%2Fv6-8A2BE2)  
[![Tinfoil Version](https://img.shields.io/badge/Tinfoil-v20.0-da1c5c)](https://tinfoil.io/Download)
[![Sphaira Version](https://img.shields.io/badge/Sphaira-v1.0.0-%233cd57a)](https://github.com/ITotalJustice/sphaira)
[![CyberFoil Version](https://img.shields.io/badge/CyberFoil-v1.4.1-firebrick)](https://github.com/luketanti/CyberFoil)


Ownfoil is a Nintendo Switch library manager, that will also turn your library into a fully customizable and self-hosted Shop, supporting multiple clients. The goal of this project is to manage your library, identify any missing content (DLCs or updates) and provide a user friendly way to browse and install your content. Some of the features include:
- [x] multi user authentication
- [x] web interface for configuration and browsing the library
- [x] content identification using content decryption or filename
- [x] automatic library organization
- [x] console keys management
- [x] multiple clients support
- [x] shop customization

# Installation

- [Using Docker](#using-docker)
- [Using Python](#using-python)
- [Using Unraid](https://preview.ca.unraid.net/apps?q=ownfoil&app=v2fayr)
- [Using Helm chart](./chart)

> [!CAUTION]
> There is __no website associated with this project__, only this GitHub repo.  
> Ownfoil is __not released as an application or an executable file__ - DO NOT download or execute anything related to Ownfoil outside of this repository and its instructions.

## Using Docker
Ownfoil is shipped as a docker container for easy deployment, data persistency and updates. If you are unfamiliar with Docker, check [the installation documentation here](https://docs.docker.com/engine/install/).  
### Docker run

<details>

Running this command will start the shop on local port `8465` with the library in `/your/game/directory`, and persist the `data` and `config` directories:
```
docker run -d -p 8465:8465 \
   -v /your/game/directory:/games \
   -v ./config:/app/config \
   -v ./data:/app/data \
   --name ownfoil \
   a1ex4/ownfoil
```
To see the logs of the container:  

      docker logs -f ownfoil

</details>

### Docker compose
<details>

Create a file named `docker-compose.yml` with the following content:
```
---
services:
  ownfoil:
    container_name: ownfoil
    image: a1ex4/ownfoil
   # environment:
   #   # For write permission in config directory
   #   - PUID=1000
   #   - PGID=1000
   #   # to create/update an admin user at startup
   #   - USER_ADMIN_NAME=admin
   #   - USER_ADMIN_PASSWORD=asdvnf!546
   #   # to create/update a regular user at startup
   #   - USER_GUEST_NAME=guest
   #   - USER_GUEST_PASSWORD=oerze!@8981
    volumes:
      - /your/game/directory:/games
      - ./data:/app/data
      - ./config:/app/config
    ports:
      - "8465:8465"
```
> [!TIP]
> You can control the `UID` and `GID` of the user running the app in the container with the `PUID` and `PGID` environment variables. By default the user is created with `1000:1000`. If you want to have the same ownership for mounted directories, you need to set those variables with the UID and GID returned by the `id` command.

You can then create and start the container with the command (executed in the same directory as the docker-compose file):

    docker-compose up -d

This is usefull if you don't want to remember the `docker run` command and have a persistent and reproductible container configuration.
</details>

## Using Python
This requires Python to be installed on your system. If that's not the case [you can use uv](https://docs.astral.sh/uv/getting-started/) to [install a Python environment](https://docs.astral.sh/uv/guides/install-python).
<details>
Download the repository as a zip archive, extract it, install the dependencies and you're good to go!

1. Download the repository code on GitHub:
   1. __Make sure you are visiting the official repo URL__ at https://github.com/a1ex4/ownfoil
   2. Above the list of files, click `<> Code`.
   3. Click `Download ZIP`.
2. Extract the zip archive and navigate to the `ownfoil-master` directory
3. Open a terminal in this folder (on Windows, `Right click` → `Open command window here`)
4. Install dependencies and run Ownfoil:
```
$ pip install -r requirements.txt
$ python app/app.py
```
</details>

# Usage
Once Ownfoil is running, the Shop Web UI is now accessible with your computer/server IP and port, by navigating to `http://<computer/server IP>:8465`, i.e. `http://localhost:8465` from the same computer or `http://192.168.1.100:8465` from a device in your network.

## Clients supported

Ownfoil supports multiple clients to install content on your Nintendo Switch:
### [Tinfoil:](https://tinfoil.io/Download)
- ✅ `HTTP` / `HTTPS` protocol support
- ✅ User authentication
- ✅ Shop browsing with icons and banners
- ✅ Content filtering (games, updates, DLC, XCI) based on URL
- ✅ New games, DLC, Updates, Recommended and XCI sections
- ✅ Compressed content (NSZ and XCZ) support
- ✅ Encrypted shop support
- ✅ Client side Host verification for secure connections
- ✅ Tinfoil shop customization

### [Sphaira:](https://github.com/ITotalJustice/sphaira)
- ✅ `HTTP` / `HTTPS` protocol support
- ✅ User authentication
- ✅ Directory-based file browsing
- ✅ Content filtering (games, updates, DLC, XCI) based on URL
- ✅ Compressed content (NSZ and XCZ) support

### [CyberFoil:](https://github.com/luketanti/CyberFoil)
- ✅ `HTTP` / `HTTPS` protocol support
- ✅ User authentication
- ✅ Shop browsing with icons and Sections (Updates, DLC)
- ✅ Compressed content (NSZ and XCZ) support
- ✅ Client side Host verification for secure connections
- ✅ Custom welcome message (MOTD)

> [!TIP]
> Check the `Setup` page in the Web UI for specific instructions on configuring each app, using local or remote access.

## User administration
Ownfoil requires an `admin` user to be created to enable Authentication for your Shop. Go to the `Settings` to create a first user that will have admin rights. Then you can add more users to your shop the same way.

## Library administration
In the `Settings` page under the `Library` section, you can add directories containing your content. You can then manually trigger the library scan: Ownfoil will scan the content of the directories and try to identify every supported file (currently `nsp`, `nsz`, `xci`, `xcz`).

> [!TIP]
> There is watchdog in place for all your configured libraries: files moved, renamed, added or removed will be reflected directly in your library.

The automatic library organization can be configured in the `Organizer` section to set your own templates, enable removing older updates...

## Titles configuration
In the `Settings` page under the `Titles` section is where you specify the language of your Shop (currently the same for all users).

This is where you can also upload your `console keys` file to enable content identification using decryption, instead of only using filenames. If you do not provide keys, Ownfoil expects the files to be named `[APP_ID][vVERSION]`.

Ownfoil will warn you if any master key is invalid or missing, to ensure all backups can be decrypted and identified.

## Shop customization
In the `Settings` page under the `Shop` section is where you customize your Shop, like the message displayed when successfully accessing the shop from Tinfoil or if the shop is private or public.
