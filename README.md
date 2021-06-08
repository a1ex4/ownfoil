# Ownfoil
Badges:
 - docker hub (pulls)
 - latest tag
 - Tinfoil version
 - Awoo version

Ownfoil is a simple webserver to run your own Tinfoil/Awoo shop from your local library, with full shop customisation and security. It is designed to periodically scan your library, generate Tinfoil index file and serve it all over HTTP/S.

Toc

## Usage
Ownfoil is shipped as a Docker container for simplicity and compatibility. You first need to [install Docker](https://docs.docker.com/get-docker/). Come back when you have a working installation !

There are two ways to start the container, with `docker run` or `docker-compose`.


### Docker run

Running this command will start the shop on port `8000` with the library in `/your/game/directory` :

    docker run --rm -p 8000:80 -v /your/game/directory:/games --name ownfoil a1ex4/ownfoil

### Docker compose
Create a file named `docker-compose.yml` with the following content:
```
version: "3"

services:
  ownfoil:
    container_name: ownfoil
    image: a1ex4/ownfoil
    volumes:
      - /your/game/directory:/games
      # Uncomment if you want to edit and persist the app configuration
      # - ./app:/app
      # Uncomment to setup basic auth
      # - ./nginx:/etc/nginx
    ports:
      - "8000:80"
```

You can then create and start the container with the command (executed in the same directory as the docker-compose file):

    docker-compose up -d

This is usefull if you don't want to remember the `docker run` command and have a persistent and reproductible container configuration.

## Shop customization
All Tinfoil shop index keys are configurable with a template file: simply download the [shop_template.jsonc](./shop_template.jsonc) (*with the same name*) to your library directory and uncomment the keys. 

## Setup Authentication
First, uncomment the "nginx" volume mount in the `docker-compose.yml` file and re-create the container (`docker-compose up -d`). You should now have a `nginx` folder next to the compose file: this folder is mounted inside the container (in `/etc/nginx`) so any changes to this folder will be reflected in the container.

Edit the `nginx/conf.d/default` file and uncomment the two lines starting with "auth_basic".



docker exec -it 

## Similar projects
