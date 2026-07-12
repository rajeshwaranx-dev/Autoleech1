FROM python:3.10
RUN apt-get update && apt-get install -y --no-install-recommends aria2 ffmpeg fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app/
RUN pip3 install -r requirements.txt
CMD ["python3", "bot.py"]
