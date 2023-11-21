FROM python:3.11-alpine

RUN apk add --update --no-cache bash nginx apache2-utils sudo

COPY requirements.txt /tmp/
RUN pip install --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

RUN set -e \
      && ln -sf /dev/stdout /var/log/nginx/access.log \
      && ln -sf /dev/stderr /var/log/nginx/error.log

COPY ./conf/nginx.conf /etc/nginx/http.d/default.conf
COPY ./app /app

EXPOSE 80
ENTRYPOINT [ "/app/run.sh" ]