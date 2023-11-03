FROM python:3.11-alpine

RUN apk update && apk add --no-cache build-base bash sudo git gcc musl-dev jpeg-dev zlib-dev libffi-dev cairo-dev pango-dev gdk-pixbuf-dev

RUN mkdir /app

COPY ./app /app
COPY ./docker/run.sh /app/run.sh

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

RUN mkdir -p /app/data
RUN git clone --depth=1 --no-checkout https://github.com/blawar/titledb.git /app/data/titledb

WORKDIR /app

ENTRYPOINT [ "/app/run.sh" ]

