from langchain_core.messages import BaseMessage
from datetime import datetime, timezone
def get_current_date():
    return datetime.now(tz=timezone.utc).isoformat()

def get_message_text(content: str | dict | list[str | dict]) -> str:
    
    if isinstance(content, str):
        return content
    elif isinstance(content, dict):
        return content.get("text", "")
    else:
        txts = [c if isinstance(c, str) else (c.get("text") or "") for c in content]
        return "".join(txts).strip()



