FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: connect to ib-gateway service on docker network
ENV IBKR_HOST=ib-gateway
ENV IBKR_PORT=4002
ENV IBKR_CLIENT_ID=1
ENV DASHBOARD_PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]
