"""
End-to-end latency test for the full voice pipeline.
Measures time from audio sent to final response received.
"""

import asyncio
import time
import httpx

BASE_URL = "http://localhost:8000"
TEST_WAV = "test.wav"


async def test_voice_pipeline(session_id: str = None):
    """Test the combined /voice endpoint with speculative execution."""

    checkpoints = {}

    async with httpx.AsyncClient(timeout=60.0) as client:
        with open(TEST_WAV, "rb") as f:
            audio_data = f.read()

        print(f"Audio file size: {len(audio_data):,} bytes")
        if session_id:
            print(f"Session ID: {session_id} (reusing for speculative fetch)")
        print("=" * 60)

        # === Send to /voice endpoint ===
        pipeline_start = time.perf_counter()
        checkpoints["pipeline_start"] = 0

        print("\n[1] VOICE: Sending audio to /api/v1/voice...")
        print("    (ASR + speculative fetch run in parallel)")

        files = {"file": ("test.wav", audio_data, "audio/wav")}
        data = {"session_id": session_id} if session_id else {}

        response = await client.post(f"{BASE_URL}/api/v1/voice", files=files, data=data)

        voice_end = time.perf_counter()
        checkpoints["voice_complete"] = voice_end - pipeline_start

        if response.status_code != 200:
            print(f"    FAILED: {response.status_code} - {response.text}")
            return None, None

        result = response.json()
        transcript = result.get("transcript", "")
        session_id = result.get("session_id")
        data = result.get("data", {})
        response_type = data.get("type", "UNKNOWN")
        payload = data.get("payload", {})
        audio_ticket = result.get("audio_ticket")

        print(f"    Transcript: \"{transcript}\"")
        print(f"    Response type: {response_type}")

        if response_type == "RESULT":
            tts_text = payload.get("tts_text", "")
            print(f"    TTS text: \"{tts_text[:80]}{'...' if len(tts_text) > 80 else ''}\"")
            departures = payload.get("departures", [])
            if departures:
                mins = departures[0].get("minutes_to_depart")
                print(f"    Next departure: {mins} min")
        elif response_type == "CLARIFICATION":
            print(f"    Question: \"{payload.get('question_text', '')}\"")
        elif response_type == "ERROR":
            print(f"    Error: \"{payload.get('message', '')}\"")

        voice_ms = (voice_end - pipeline_start) * 1000
        print(f"    Voice latency: {voice_ms:.0f}ms")

        # --- TTS Retrieval (optional) ---
        tts_latency = None
        if audio_ticket:
            print(f"\n[2] TTS: Polling /api/v1/media/{audio_ticket}...")
            tts_start = time.perf_counter()

            for i in range(50):
                tts_response = await client.get(f"{BASE_URL}/api/v1/media/{audio_ticket}")

                if tts_response.status_code == 200:
                    tts_end = time.perf_counter()
                    checkpoints["tts_complete"] = tts_end - pipeline_start
                    audio_size = len(tts_response.content)
                    tts_latency = (tts_end - tts_start) * 1000
                    print(f"    Audio received: {audio_size:,} bytes")
                    print(f"    TTS latency: {tts_latency:.0f}ms")
                    break
                elif tts_response.status_code == 202:
                    await asyncio.sleep(0.2)
                else:
                    print(f"    TTS FAILED: {tts_response.status_code}")
                    break
        else:
            print("\n[2] TTS: No audio ticket")

        # === Summary ===
        pipeline_end = time.perf_counter()
        total_ms = (pipeline_end - pipeline_start) * 1000

        print("\n" + "=" * 60)
        print("LATENCY SUMMARY")
        print("=" * 60)
        print(f"\n{'Stage':<30} {'Latency':>10}")
        print("-" * 42)
        print(f"{'Voice (ASR + Agent)':<30} {voice_ms:>9.0f}ms")
        if tts_latency:
            print(f"{'TTS (Azure)':<30} {tts_latency:>9.0f}ms")
        print("-" * 42)
        print(f"{'TOTAL':<30} {total_ms:>9.0f}ms")

        return session_id, checkpoints


async def test_speculative_execution():
    """Test two requests - second should benefit from speculative fetch."""

    print("=" * 60)
    print("TEST 1: First request (cold - no history)")
    print("=" * 60)

    session_id, _ = await test_voice_pipeline()

    if not session_id:
        print("First request failed")
        return

    print("\n\n")
    print("=" * 60)
    print("TEST 2: Second request (warm - with speculative fetch)")
    print("=" * 60)
    print("(If first request succeeded, pre-fetched data should speed up matching queries)")

    await test_voice_pipeline(session_id)


if __name__ == "__main__":
    print("PTV Notify - Voice Pipeline Latency Test")
    print("=" * 60)
    asyncio.run(test_speculative_execution())
