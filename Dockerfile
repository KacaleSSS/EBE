FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY ebe ./ebe
RUN pip install --no-cache-dir '.[pdf,images]' && useradd --uid 10001 --create-home ebe
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /data
ENTRYPOINT ["ebe"]
CMD ["doctor"]
