# 1. Use the official lightweight Python 3.15 image
FROM python:3.15-slim

# 2. Prevent Python from writing .pyc files to disk and ensure logs print immediately
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Set the working directory inside the container
WORKDIR /app

# 4. Copy ONLY the dependencies list first (improves build speeds)
COPY requirements.txt .

# 5. Install the Python packages
RUN pip install --no-cache-dir -r requirements.txt

# 6. "Upload" the rest of your local files into the container
COPY . .

# 7. Expose the port your website runs on (e.g., 5000 for Flask, 8000 for Django)
EXPOSE 5000

# 8. The command to run your website when the container starts
CMD ["python", "app.py"]
