FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# BOT_TOKEN передавать при запуске: docker run -e BOT_TOKEN=... memebot
CMD ["python", "bot.py"]
