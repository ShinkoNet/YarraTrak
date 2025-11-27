import time

# Simple in-memory store for conversation context: {session_id: {"history": [], "last_active": timestamp}}
# Query history for speculative fetch is now client-side (localStorage)
_sessions = {}

MAX_HISTORY = 4
SESSION_TTL = 120

def get_session(session_id: str):
    """Retrieve session data, creating if not exists."""
    now = time.time()
    
    # Cleanup old sessions occasionally
    if len(_sessions) > 100:
        _cleanup_sessions(now)
        
    if session_id not in _sessions:
        _sessions[session_id] = {"history": [], "last_active": now}
    
    _sessions[session_id]["last_active"] = now
    return _sessions[session_id]

def update_history(session_id: str, role: str, content: str):
    """Add a message to history, keeping only the last N turns."""
    session = get_session(session_id)
    history = session["history"]
    
    history.append({"role": role, "content": content})
    
    # Trim history (keep last N pairs roughly)
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]
        
    session["history"] = history

def get_history(session_id: str):
    return get_session(session_id)["history"]


def _cleanup_sessions(now):
    """Remove expired sessions."""
    expired = [sid for sid, data in _sessions.items() if now - data["last_active"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]
