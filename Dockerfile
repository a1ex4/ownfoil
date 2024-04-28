FROM python:3.11-alpine

RUN apk update && apk add --no-cache bash sudo git

RUN mkdir /app

COPY ./app /app
COPY ./docker/run.sh /app/run.sh

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

RUN mkdir -p /app/data

WORKDIR /app

ENTRYPOINT [ "/app/run.sh" ]

