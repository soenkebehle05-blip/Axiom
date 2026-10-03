from pydantic import BaseModel


class ChatIn(BaseModel):
    text: str
    session_id: str | None = None
    timezone: str | None = None
