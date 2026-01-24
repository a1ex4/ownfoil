FROM python:3.11-alpine

# Install platform-specific build dependencies
ARG TARGETPLATFORM
RUN apk update && apk add --no-cache bash sudo \
    && if [ "$TARGETPLATFORM" = "linux/arm/v6" ] || [ "$TARGETPLATFORM" = "linux/arm/v7" ]; then \
        apk add --no-cache build-base gcc musl-dev jpeg-dev zlib-dev libffi-dev cairo-dev pango-dev gdk-pixbuf-dev; \
    fi

RUN mkdir /app

COPY ./app /app
COPY ./nsz /nsz
COPY ./docker/run.sh /app/run.sh

COPY requirements.txt /tmp/

RUN pip install --no-cache-dir --requirement /tmp/requirements.txt && rm /tmp/requirements.txt

RUN if [ "$TARGETPLATFORM" = "linux/arm/v6" ] || [ "$TARGETPLATFORM" = "linux/arm/v7" ]; then \
        apk del build-base gcc musl-dev jpeg-dev zlib-dev libffi-dev cairo-dev pango-dev gdk-pixbuf-dev; \
    fi

RUN mkdir -p /app/data

WORKDIR /app

ENTRYPOINT [ "/app/run.sh" ]

