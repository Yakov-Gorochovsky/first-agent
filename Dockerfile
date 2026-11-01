FROM python:3.12-slim

RUN useradd --create-home --uid 10001 swarm
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p workspace && chown -R swarm:swarm /app

USER swarm
ENV HOST=0.0.0.0
EXPOSE 8000

# run_python needs a container runtime, so inside this image it stays disabled (fail closed).
CMD ["python", "server.py"]
