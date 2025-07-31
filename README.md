# Ownfoil
[![Latest Release](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)](https://github.com/a1ex4/ownfoil/releases/latest)
[![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil)](https://hub.docker.com/r/a1ex4/ownfoil)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil?sort=date&arch=amd64)](https://hub.docker.com/r/a1ex4/ownfoil/tags)

Ownfoil is a Nintendo Switch library manager, that will also turn your library into a fully customizable and self-hosted Tinfoil Shop. The goal of this project is to manage your library, identify any missing content (DLCs or updates) and provide a user friendly way to browse your content. Some of the features include:

 - multi user authentication
 - web interface for configuration
 - web interface for browsing the library
 - content identification using decryption or filename
 - Tinfoil shop customization

The project is still in development, expect things to break or change without notice.

# Table of Contents
- [Installation](#nstallation)
- [Usage](#usage)
- [Roadmap](#roadmap)
- [Similar Projects](#similar-projects)

# Installation
## Using Docker
### Docker run

Running this command will start the shop on port `8465` with the library in `/your/game/directory` :

    docker run -d -p 8465:8465 -v /your/game/directory:/games -v /your/config/directory:/app/config --name ownfoil a1ex4/ownfoil

The shop is now accessible with your computer/server IP and port, i.e. `http://localhost:8465` from the same computer or `http://192.168.1.100:8465` from a device in your network.

### Docker compose
Create a file named `docker-compose.yml` with the following content:
```
version: "3"

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
      - ./config:/app/config
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
$ git clone --recurse-submodules https://github.com/a1ex4/ownfoil
$ cd ownfoil
$ pip install -r requirements.txt
$ python app/app.py
```
To update the app you will need to pull the latest commits.

## Tinfoil setup
In Tinfoil, add a shop with the following settings:
 - Protocol: `http` (or `https` if using a SSL enabled reverse proxy)
 - Host: server/computer IP, i.e. `192.168.1.100`
 - Port: host port of the container, i.e. `8000`
 - Username: username as created in Ownfoil settings (if the shop is set to Private)
 - Password: password as created in Ownfoil settings (if the shop is set to Private)

# Usage
Once Ownfoil is running you can access the Shop Web UI by navigating to the `http://<computer/server IP>:8465`.

## User administration
Ownfoil requires an `admin` user to be created to enable Authentication for your Shop. Go to the `Settings` to create a first user that will have admin rights. Then you can add more users to your shop the same way.

## Library administration
In the `Settings` page under the `Library` section, you can add directories containing your content. You can then manually trigger the library scan: Ownfoil will scan the content of the directories and try to identify every supported file (currently `nsp`, `nsz`, `xci`, `xcz`).
There is watchdog in place for all your added directories: files moved, renamed, added or removed will be reflected directly in your library.

## Titles configuration
In the `Settings` page under the `Titles` section is where you specify the language of your Shop (currently the same for all users).

This is where you can also upload your `console keys` file to enable content identification using decryption, instead of only using filenames. If you do not provide keys, Ownfoil expects the files to be named `[APP_ID][vVERSION]`.

## Shop customization
In the `Settings` page under the `Shop` section is where you customize your Shop, like the message displayed when successfully accessing the shop from Tinfoil or if the shop is private or public.

# Roadmap
Planned feature, in no particular order.
 - Library browser:
    - [ ] Add "details" view for every content, to display versions etc
 - Library management:
    - [ ] Rename and organize library after content identification
    - [ ] Delete older updates
    - [ ] Automatic nsp/xci -> nsz conversion
 - Shop customization:
    - [ ] Encrypt shop
 - Support emulator Roms
    - [ ] Scrape box arts
    - [ ] Automatically create NSP forwarders
 - Saves manager:
    - [ ] Automatically discover Swicth device based on Tinfoil connection
    - [ ] Only backup and serve saves based on the user/Switch
 - External services:
    - [ ] Integrate torrent indexer Jackett to download updates automatically

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
