FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml chainlit.md ./
COPY src ./src
COPY dashboards ./dashboards
COPY public ./public
RUN pip install .
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8000
CMD ["chainlit", "run", "src/finops_nl/app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
