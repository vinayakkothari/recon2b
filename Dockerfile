FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[web]"

RUN useradd --create-home recon
USER recon
EXPOSE 8000
CMD ["recon2b", "serve", "--host", "0.0.0.0", "--port", "8000"]
