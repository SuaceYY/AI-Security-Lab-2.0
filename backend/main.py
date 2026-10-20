from fastapi import FastAPI

app = FastAPI(title="AI-Security-Lab 2.0", version="0.1.0")

@app.get("/")
def home():
    return {"message": "AI-Security-Lab 2.0 API is running"}

@app.get("/health")
def health():
    return {"status": "ok"}
