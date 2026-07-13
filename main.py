"""Voice Companion backend — Step 2: mode router.

POST /chat accepts transcript + mode + user_id, loads the matching system
prompt from MODE_REGISTRY, calls Claude, and returns spoken text.
"""
import os
from datetime import date, datetime

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from modes.author.prompt import SYSTEM_PROMPT as AUTHOR_PROMPT
from modes.health.prompt import SYSTEM_PROMPT as HEALTH_PROMPT
from modes.jarvis.prompt import SYSTEM_PROMPT as JARVIS_PROMPT

load_dotenv()

app = FastAPI(title="Lifesight Backend")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

MODE_REGISTRY = {
    "author": AUTHOR_PROMPT,
    "health": HEALTH_PROMPT,
    "jarvis": JARVIS_PROMPT,
}


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "author"
    user_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    mode: str


def _build_system_prompt(mode: str) -> str:
    today = date.today().isoformat()
    now_local = datetime.now().strftime("%A %B %d, %Y at %I:%M %p").replace(" 0", " ")
    return (
        f"{MODE_REGISTRY[mode]}\n\n"
        f"Today's date is {today}. Current local time: {now_local}."
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/modes")
def modes():
    return {"modes": sorted(MODE_REGISTRY)}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    mode = req.mode.lower().strip()
    if mode not in MODE_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported mode '{mode}'. Valid modes: {sorted(MODE_REGISTRY)}",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_build_system_prompt(mode),
        messages=[{"role": "user", "content": req.transcript}],
    )

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    return ChatResponse(response=text_blocks[0].strip(), mode=mode)
