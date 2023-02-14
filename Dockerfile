FROM python:3.9-alpine

RUN mkdir /app
RUN mkdir /games

COPY ./app /app
COPY ./conf/nginx.conf /etc/nginx/http.d/default.conf

RUN apk add --update --no-cache bash nginx openssl apache2-utils sudo

RUN pip3 install --no-cache --upgrade jsonc-parser

RUN set -e \
      && ln -sf /dev/stdout /var/log/nginx/access.log \
      && ln -sf /dev/stderr /var/log/nginx/error.log

EXPOSE 80
ENTRYPOINT [ "/app/run.sh" ]