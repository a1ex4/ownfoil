FROM python:3.11-alpine

RUN apk update && apk add --no-cache build-base bash sudo

RUN mkdir /app

COPY ./app /app
COPY ./docker/run.sh /app/run.sh

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

RUN mkdir -p /app/data
ADD https://raw.githubusercontent.com/blawar/titledb/master/cnmts.json /app/data/cnmts.json
ADD https://raw.githubusercontent.com/blawar/titledb/master/versions.json /app/data/versions.json
ADD https://raw.githubusercontent.com/blawar/titledb/master/US.en.json /app/data/US.en.json

WORKDIR /app

ENTRYPOINT [ "/app/run.sh" ]

