FROM python:3.9-alpine

RUN mkdir /app
RUN mkdir /games

COPY ./libs /app
COPY ./nginx.conf /etc/nginx/http.d/default.conf
RUN touch /etc/nginx/.htpasswd

RUN apk add --update --no-cache nginx openssl
RUN pip3 install --no-cache --upgrade pip setuptools jsonc-parser

RUN set -e \
      && mkdir /run/nginx \
      && ln -sf /dev/stdout /var/log/nginx/access.log \
      && ln -sf /dev/stderr /var/log/nginx/error.log

EXPOSE 80

ENTRYPOINT nginx -g "daemon on;" && python /app/gen_shop.py /games
