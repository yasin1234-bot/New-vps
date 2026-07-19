FROM python:3.10-slim

# ২. psutil এর জন্য প্রয়োজনীয় C লাইব্রেরি ইনস্টল করা (যদি psutil লাগে)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ৩. ওয়ার্কিং ডিরেক্টরি সেট করা
WORKDIR /app

# ৪. ফাইলগুলো ডকার ইমেজে কপি করা
COPY requirements.txt .

# ৫. ডিপেন্ডেন্সি ইনস্টল করা
RUN pip install --no-cache-dir -r requirements.txt

# ৬. বাকি সব কোড কপি করা
COPY . .

# ৭. অ্যাপ রান করার কমান্ড (রেলওয়ে এটি ড্যাশবোর্ড থেকেও হ্যান্ডেল করতে পারে)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "120"]
