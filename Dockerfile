# পাইথনের অফিসিয়াল ইমেজ ব্যবহার করা হচ্ছে (সঠিক ট্যাগসহ)
FROM python:3.11.0-slim

# কাজের ডিরেক্টরি সেট করা
WORKDIR /app

# সিস্টেমের প্রয়োজনীয় টুলস ইনস্টল করা
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt ফাইলটি কপি করা
COPY requirements.txt .

# লাইব্রেরিগুলো ইনস্টল করা
RUN pip install --no-cache-dir -r requirements.txt

# প্রজেক্টের সব ফাইল কপি করা
COPY . .

# পোর্টের জন্য এনভায়রনমেন্ট ভেরিয়েবল (Render সাধারণত $PORT ব্যবহার করে)
ENV PORT=8080

# অ্যাপ রান করার কমান্ড
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT app:app"]
