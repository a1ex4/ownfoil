# Ownfoil

[![Latest Release](https://img.shields.io/docker/v/luketanti/ownfoil?sort=semver)](https://github.com/luketanti/ownfoil/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/luketanti/ownfoil)](https://hub.docker.com/r/luketanti/ownfoil)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/luketanti/ownfoil?sort=date&arch=amd64)](https://hub.docker.com/r/luketanti/ownfoil/tags)  
![Static Badge](https://img.shields.io/badge/platforms-amd64%20%7C%20%20arm64%2Fv8%20%7C%20arm%2Fv7%20%7C%20arm%2Fv6-8A2BE2)

Ownfoil is a Nintendo Switch library manager that turns your library into a fully customizable, self-hosted Tinfoil Shop. The goal of this project is to manage your library, identify any missing content (DLCs or updates) and provide a user friendly way to browse your content. Some of the features include:

 - multi user authentication
 - web interface for configuration
 - web interface for browsing the library
 - content identification using decryption or filename
 - Tinfoil shop customization

The project is still in development, expect things to break or change without notice.

# Table of Contents
- [Installation](#installation)
- [Usage](#usage)
- [Roadmap](#roadmap)

# Installation
## Using Docker
### Docker run

Running this command will start the shop on port `8465` with the library in `/your/game/directory`:

    docker run -d -p 8465:8465 \
      -v /your/game/directory:/games \
      -v /your/config/directory:/app/config \
      -v /your/data/directory:/app/data \
      --name ownfoil \
      luketanti/ownfoil:latest

The shop is now accessible with your computer/server IP and port, i.e. `http://localhost:8465` from the same computer or `http://192.168.1.100:8465` from a device in your network.

### Docker compose
Create a file named `docker-compose.yml` with the following content:
```
version: "3"

services:
  ownfoil:
    container_name: ownfoil
    image: luketanti/ownfoil:latest
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
      - ./config:/app/config
      - ./data:/app/data
    ports:
      - "8465:8465"
```
> [!NOTE]
> You can control the `UID` and `GID` of the user running the app in the container with the `PUID` and `PGID` environment variables. By default the user is created with `1000:1000`. If you want to have the same ownership for mounted directories, you need to set those variables with the UID and GID returned by the `id` command.

You can then create and start the container with the command (executed in the same directory as the docker-compose file):

    docker-compose up -d

This is usefull if you don't want to remember the `docker run` command and have a persistent and reproductible container configuration.

## Using Python
Clone the repository using `git`, install the dependencies and you're good to go:
```
$ git clone https://github.com/luketanti/ownfoil
$ cd ownfoil
$ pip install -r requirements.txt
$ python app/app.py
```
To update the app you will need to pull the latest commits.

## CyberFoil setup
In CyberFoil, set the Ownfoil eShop URL in Settings:
 - URL: `http://<server-ip>:8465` (or `https://` if using an SSL-enabled reverse proxy)
 - Username: username as created in Ownfoil settings (if the shop is Private)
 - Password: password as created in Ownfoil settings (if the shop is Private)

# Usage
Once Ownfoil is running you can access the Shop Web UI by navigating to the `http://<computer/server IP>:8465`.

## User administration
Ownfoil requires an `admin` user to be created to enable Authentication for your Shop. Go to the `Settings` to create a first user that will have admin rights. Then you can add more users to your shop the same way.

## Library administration
In the `Settings` page under the `Library` section, you can add directories containing your content. You can then manually trigger the library scan: Ownfoil will scan the content of the directories and try to identify every supported file (currently `nsp`, `nsz`, `xci`, `xcz`).
There is watchdog in place for all your added directories: files moved, renamed, added or removed will be reflected directly in your library.

## Library management
In the `Manage` page, you can organize your library structure, delete older update files, and convert `nsp`/`xci` to `nsz`.

Conversion details:
- Uses the bundled `nsz` tool from the `./nsz` directory (with progress output).
- Uses the same `keys.txt` uploaded in the `Settings` page.
- Shows live status, per-file progress, and the current filename.
- Filters out files smaller than 50 MB from the manual conversion dropdown.
- The `Verbose` checkbox shows detailed task output; otherwise the task output stays clean.

## Automatic update downloads (Prowlarr + Torrent Client)
Ownfoil can automatically search for missing updates using Prowlarr, send matches to a torrent client (qBittorrent or Transmission), and ingest completed downloads back into the library. The UI is modeled after apps like Sonarr/Radarr with explicit connection tests.

### Setup
1. Open the `Settings` page and scroll to the **Downloads** section.
2. Enable **Automatic downloads** and configure:
   - **Search interval (minutes)**: how often Ownfoil will look for missing updates.
   - **Minimum seeders**: skip low‑availability results.
   - **Required terms / Blacklist terms**: fine‑tune search matches (comma separated).
   - **Torrent category/tag**: used to tag downloads in the client (default `ownfoil`).
3. Configure **Prowlarr**:
   - **Prowlarr URL** (e.g. `http://localhost:9696`)
   - **API Key**
   - **Indexer IDs** (optional, comma separated). If set, Ownfoil will limit searches to these indexers.
   - Use **Test Prowlarr** to validate connectivity and indexer IDs (missing IDs show as warnings).
4. Configure **Torrent Client**:
   - **Client**: qBittorrent or Transmission.
   - **Client URL** and credentials.
   - **Download path** (optional): if set, Ownfoil will warn if it doesn't exist or isn't writable.
   - Use **Test torrent client** to validate connectivity.

### Notes
- Prowlarr is used for searching and ranking results; the torrent client handles the actual downloads.
- Warnings do not block tests; they highlight misconfigurations (e.g. missing indexer IDs or invalid download paths).
- The downloader runs on a schedule and respects the configured interval, skipping runs if the interval has not elapsed.
- Completed downloads are detected by category/tag and trigger a library scan + refresh.

## Titles configuration
In the `Settings` page under the `Titles` section is where you specify the language of your Shop (currently the same for all users).

This is where you can also upload your `console keys` file to enable content identification using decryption, instead of only using filenames. If you do not provide keys, Ownfoil expects the files to be named `[APP_ID][vVERSION]`.

## Shop customization
In the `Settings` page under the `Shop` section is where you customize your Shop, like the message displayed when successfully accessing the shop from Tinfoil or if the shop is private or public.
The `Encrypt shop` option only affects the Tinfoil payload; the web interface and admin UI remain accessible as normal.
Encryption uses the Tinfoil public key and AES, and requires the `pycryptodome` dependency.

# Deployment notes
- Recommended volumes: `/games`, `/app/config`, and `/app/data`.
- Map port `8465` from the container to any host port you prefer.
- To bootstrap an admin account, set `USER_ADMIN_NAME` and `USER_ADMIN_PASSWORD` when starting the container.
- Update the container with `docker pull luketanti/ownfoil:latest` and restart it.

# Roadmap
Planned feature, in no particular order.
 - Library browser:
    - [x] Add "details" view for every content, to display versions etc
 - Library management:
    - [x] Rename and organize library after content identification
    - [x] Delete older updates
    - [x] Automatic nsp/xci -> nsz conversion
 - Shop customization:
    - [x] Encrypt shop
 - Support emulator Roms
    - [ ] Scrape box arts
    - [ ] Automatically create NSP forwarders
 - Saves manager:
    - [ ] Automatically discover Switch device based on Tinfoil connection
    - [ ] Only backup and serve saves based on the user/Switch
 - External services:
    - [x] Prowlarr integration for automatic update downloads (via torrent client)
    - [x] Automated update downloader pipeline (search -> download -> ingest)
