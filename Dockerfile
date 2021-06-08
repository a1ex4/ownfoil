# a1ex4/ownfoil:1.0.0
FROM alpine:latest

RUN mkdir /app
RUN mkdir /games

COPY ./libs /app
COPY ./nginx.conf /etc/nginx/conf.d/default.conf
RUN touch /etc/nginx/.htpasswd

RUN apk add --update --no-cache openssl python3 && ln -sf python3 /usr/bin/python
RUN python3 -m ensurepip
RUN pip3 install --no-cache --upgrade pip setuptools jsonc-parser

RUN set -e \
      && apk add --update --no-cache nginx

RUN set -e \
      && mkdir /run/nginx \
      && ln -sf /dev/stdout /var/log/nginx/access.log \
      && ln -sf /dev/stderr /var/log/nginx/error.log

EXPOSE 80

ENTRYPOINT nginx -g "daemon on;" && python /app/gen_shop.py /games
