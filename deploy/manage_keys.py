#!/usr/bin/env python3
"""
API Key Management for PTV Notify

Usage:
    python manage_keys.py list           # Show all current keys
    python manage_keys.py generate       # Generate and add a new key
    python manage_keys.py generate "username"  # Generate key with label
    python manage_keys.py remove KEY     # Remove a specific key
"""

import os
import sys
import secrets
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"


def load_env():
    """Load .env file as dict."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def save_env(env):
    """Save dict back to .env file."""
    lines = []
    for key, value in env.items():
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n")


def get_keys():
    """Get current API keys as list."""
    env = load_env()
    keys_str = env.get("API_KEYS", "")
    return [k.strip() for k in keys_str.split(",") if k.strip()]


def set_keys(keys):
    """Update API_KEYS in .env."""
    env = load_env()
    env["API_KEYS"] = ",".join(keys)
    save_env(env)


def generate_key():
    """Generate a secure random API key."""
    return secrets.token_hex(32)


def cmd_list():
    """List all API keys."""
    keys = get_keys()
    if not keys:
        print("No API keys configured.")
        print("Run: python manage_keys.py generate")
        return
    
    print(f"Found {len(keys)} API key(s):\n")
    for i, key in enumerate(keys, 1):
        # Show first 8 and last 4 chars for identification
        masked = f"{key[:8]}...{key[-4:]}"
        print(f"  {i}. {masked}")
    print()


def cmd_generate(label=None):
    """Generate a new API key."""
    key = generate_key()
    keys = get_keys()
    keys.append(key)
    set_keys(keys)
    
    label_str = f" for '{label}'" if label else ""
    print(f"Generated new API key{label_str}:\n")
    print(f"  {key}")
    print()
    print("Share this key with the user. They enter it in Pebble settings.")
    print(f"Total keys: {len(keys)}")
    print()
    print("⚠️  Restart the server to apply: systemctl restart netcavy-ptv")


def cmd_remove(key_or_index):
    """Remove an API key."""
    keys = get_keys()
    
    # Check if it's an index
    try:
        idx = int(key_or_index) - 1
        if 0 <= idx < len(keys):
            removed = keys.pop(idx)
            set_keys(keys)
            print(f"Removed key: {removed[:8]}...{removed[-4:]}")
            print(f"Remaining keys: {len(keys)}")
            print("\n⚠️  Restart the server to apply: systemctl restart netcavy-ptv")
            return
    except ValueError:
        pass
    
    # Check if it's a full key
    if key_or_index in keys:
        keys.remove(key_or_index)
        set_keys(keys)
        print(f"Removed key: {key_or_index[:8]}...{key_or_index[-4:]}")
        print(f"Remaining keys: {len(keys)}")
        print("\n⚠️  Restart the server to apply: systemctl restart netcavy-ptv")
        return
    
    print(f"Key not found: {key_or_index}")
    print("Use 'list' to see current keys.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1].lower()
    
    if cmd == "list":
        cmd_list()
    elif cmd == "generate":
        label = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_generate(label)
    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: python manage_keys.py remove KEY_OR_INDEX")
            return
        cmd_remove(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
