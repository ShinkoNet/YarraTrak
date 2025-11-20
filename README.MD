to launch the server:
The server mounts the web directory at /
```
python -m uvicorn server.api:app --host 0.0.0.0 --port 8000 --reload
```
