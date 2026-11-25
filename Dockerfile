FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sesame

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY bot.py .
COPY sesame/ sesame/

ARG SESAME_VERSION=dev
ENV SESAME_VERSION=${SESAME_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

# Sin privilegios de root dentro del contenedor.
USER sesame
CMD ["python", "-O", "bot.py"]
