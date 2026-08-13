# <img src="https://github.com/user-attachments/assets/3cfdf010-50c3-41ae-aa86-e31b22466686" height="28"> Ownfoil
[![Static Badge](https://img.shields.io/badge/github-repo-blue?logo=github)](https://github.com/a1ex4/ownfoil)
[![Latest Release](https://img.shields.io/docker/v/a1ex4/ownfoil?sort=semver)](https://github.com/a1ex4/ownfoil/releases/latest)
[![Docker Image Size (latest semver)](https://img.shields.io/docker/image-size/a1ex4/ownfoil?sort=date&arch=amd64)](https://hub.docker.com/r/a1ex4/ownfoil/tags)  
[![Docker Pulls](https://img.shields.io/docker/pulls/a1ex4/ownfoil?)](https://hub.docker.com/r/a1ex4/ownfoil)
[![Unraid downloads](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fca.unraid.net%2Fapi%2Fsearch%3Fquery%3Downfoil%26type%3Ddocker&query=%24.hits%5B0%5D.chartData.totalDownloadsChart.data%5B6%5D&label=unraid%20downloads&color=F15A2C)](https://ca.unraid.net/apps/ownfoil-19wo90o0t5ul8s)  
![Image archs](https://img.shields.io/badge/platforms-amd64%20%7C%20%20arm64%2Fv8%20%7C%20arm%2Fv7%20%7C%20arm%2Fv6-8A2BE2)  
[![Tinfoil Version](https://img.shields.io/badge/Tinfoil-v20.0-da1c5c)](https://tinfoil.io/Download)
[![Sphaira Version](https://img.shields.io/badge/Sphaira-v1.0.0-%233cd57a)](https://github.com/ITotalJustice/sphaira)
[![CyberFoil Version](https://img.shields.io/badge/CyberFoil-v1.4.1-firebrick)](https://github.com/luketanti/CyberFoil)


Ownfoil is a Nintendo Switch library manager, that will also turn your library into a fully customizable and self-hosted Shop, supporting multiple clients. The goal of this project is to manage your library, identify any missing content (DLCs or updates) and provide a user friendly way to browse and install your content. Some of the features include:
- multi user authentication
- web interface for configuration and browsing the library
- content identification using content decryption or filename
- automatic library organization
- console keys management
- multiple clients support
- shop customization

# Installation

Head over to [Install.md](./Install.md) for the full instructions:

- [Using Docker](./Install.md#using-docker)
- [Using uv (Windows users do this)](./Install.md#using-uv)
- [Using Unraid](./Install.md#using-unraid)
- [Using Proxmox LXC](./Install.md#using-proxmox-lxc)
- [Using the Helm chart](./Install.md#using-the-helm-chart)

> [!CAUTION]
> There is __no website associated with this project__, only this GitHub repo.  
> Ownfoil is __not released as an application or an executable file__ - DO NOT download or execute anything related to Ownfoil outside of this repository and its instructions.

# Usage

Configuring your shop, your clients and every setting available is documented in [Usage.md](./Usage.md) - start with [First steps](./Usage.md#first-steps), or jump straight to the [settings reference](./Usage.md#settings-reference).

# Credits

Thanks to the following projects and maintainers for making Ownfoil possible:
- @blawar for Tinfoil, Fs, TitleDB
- @nicoboss for [nsz](https://github.com/nicoboss/nsz)
- @seiya-dev for [NSTools](https://github.com/seiya-dev/NSTools)
