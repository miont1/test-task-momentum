from fastapi import FastAPI

app = FastAPI(
    title="AI-First Helpdesk Assistant",
    version="0.1.0",
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}

