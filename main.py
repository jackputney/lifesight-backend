"""Voice Companion backend — Step 1 skeleton.

POST /chat accepts transcript + mode + user_id, calls Claude, returns spoken text.
Author Mode is hardcoded here; modes/ scaffolding exists but is not wired yet.
"""
import os
from datetime import date, datetime

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="Lifesight Backend")
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Step 1: hardcoded Author Mode prompt. Next step: import from modes/.
AUTHOR_SYSTEM_PROMPT = """You are Olivia, a voice-first writing assistant in Author Mode. \
You help a visually impaired author work on their manuscript.

Your replies are shown on screen AND read aloud. Write for the ear: short sentences, \
plain text only, no markdown, bullets, headers, or emoji.

Workflow:
- CHECK: summarize or review manuscript sections the user asks about.
- WRITE: compose prose from the user's dictation in their voice.
- READ BACK: after any write, read back exactly what was added for verification.

Hard rules:
- Never invent manuscript content.
- Writes require confirmation before committing.
- Keep spoken summaries short."""

SUPPORTED_MODES = {"author"}


class ChatRequest(BaseModel):
    transcript: str = Field(..., min_length=1)
    mode: str = "author"
    user_id: str = "default"


class ChatResponse(BaseModel):
    response: str
    mode: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured")

    mode = req.mode.lower().strip()
    if mode not in SUPPORTED_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported mode '{mode}'. Supported: {sorted(SUPPORTED_MODES)}",
        )

    today = date.today().isoformat()
    now_local = datetime.now().strftime("%A %B %d, %Y at %I:%M %p").replace(" 0", " ")
    system = f"{AUTHOR_SYSTEM_PROMPT}\n\nToday's date is {today}. Current local time: {now_local}."

    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": req.transcript}],
    )

    text_blocks = [block.text for block in message.content if block.type == "text"]
    if not text_blocks:
        raise HTTPException(status_code=502, detail="Model returned no text")

    return ChatResponse(response=text_blocks[0].strip(), mode=mode)
