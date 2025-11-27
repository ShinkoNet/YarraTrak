const API_BASE = "/api/v1";
const SESSION_KEY = "ptv_session_id";
const QUERY_HISTORY_KEY = "ptv_query_history";
const MAX_QUERY_HISTORY = 10;

let currentSessionId = localStorage.getItem(SESSION_KEY) || crypto.randomUUID();
localStorage.setItem(SESSION_KEY, currentSessionId);

// --- Query History for Speculative Fetch ---
function getQueryHistory() {
    try {
        return JSON.parse(localStorage.getItem(QUERY_HISTORY_KEY)) || [];
    } catch {
        return [];
    }
}

function addToQueryHistory(stopInfo) {
    if (!stopInfo || !stopInfo.stop_id) return;

    let history = getQueryHistory();

    // Remove duplicate if exists
    history = history.filter(h =>
        !(h.stop_id === stopInfo.stop_id && h.route_type === stopInfo.route_type)
    );

    // Add to front (most recent)
    history.unshift(stopInfo);

    // Trim to max size
    if (history.length > MAX_QUERY_HISTORY) {
        history = history.slice(0, MAX_QUERY_HISTORY);
    }

    localStorage.setItem(QUERY_HISTORY_KEY, JSON.stringify(history));
}

// --- Button Configuration Logic ---
function saveButtonConfig(index, config) {
    const key = `ptv_btn_${index}`;
    localStorage.setItem(key, JSON.stringify(config));
    updateButtonUI(index, config);
}

function loadButtonConfig() {
    for (let i = 1; i <= 3; i++) {
        const key = `ptv_btn_${i}`;
        const saved = localStorage.getItem(key);
        if (saved) {
            try {
                const config = JSON.parse(saved);
                updateButtonUI(i, config);
            } catch (e) {
                console.error("Error parsing button config", e);
            }
        }
    }
}

function getButtonConfig(index) {
    const key = `ptv_btn_${index}`;
    const saved = localStorage.getItem(key);
    if (saved) {
        return JSON.parse(saved);
    }
    return null;
}

function updateButtonUI(index, config) {
    const btn = document.getElementById(`btn-${index}`);
    if (btn) {
        let name = config.stop_name || `Button ${index}`;
        if (name.length > 15) name = name.substring(0, 13) + "..";
        btn.innerText = name;
        btn.title = config.direction_name ? `${config.stop_name} -> ${config.direction_name}` : config.stop_name;
    }
}
// ----------------------------------

async function sendStealth(id) {
    const config = getButtonConfig(id);
    if (!config) {
        log(`Button ${id} not configured. Ask the agent to set it up.`, "system");
        updateStatus("Not Set");
        return;
    }

    log(`Checking ${config.stop_name}...`, "system");
    updateStatus("Checking...");

    try {
        const res = await fetch(`${API_BASE}/stealth`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                button_id: id,
                stop_id: config.stop_id,
                stop_name: config.stop_name,
                direction_id: config.direction_id,
                direction_name: config.direction_name,
                route_type: config.route_type || 0
            })
        });
        const data = await res.json();

        if (data.vibration && data.vibration.length > 0) {
            log(data.message || "Done", "system");
            updateStatus(data.message || "Done");

            // Visual Feedback
            document.body.style.backgroundColor = "#555";
            setTimeout(() => document.body.style.backgroundColor = "#333", 200);

            if (navigator.vibrate) {
                navigator.vibrate(data.vibration);
            }
        } else {
            log(data.message || "No data", "system");
            updateStatus(data.message || "No data");
        }
    } catch (e) {
        console.error(e);
        log(`Error: ${e.message}`, "system");
        updateStatus("Error");
    }
}

let mediaRecorder;
let audioChunks = [];
let isRecording = false;

async function toggleMic() {
    const micBtn = document.querySelector('.mic-btn');

    if (!isRecording) {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            mediaRecorder = new MediaRecorder(stream);
            audioChunks = [];

            mediaRecorder.ondataavailable = event => {
                audioChunks.push(event.data);
            };

            mediaRecorder.onstop = async () => {
                const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                await sendAudioToVoice(audioBlob);
            };

            mediaRecorder.start();
            isRecording = true;
            micBtn.classList.add('recording');
            micBtn.innerText = "⏹️";
            log("System: Listening...", "system");
            updateStatus("Listening...");

        } catch (err) {
            console.error("Error accessing microphone:", err);
            log("Error: Could not access microphone.", "system");
        }
    } else {
        if (mediaRecorder && mediaRecorder.state !== "inactive") {
            mediaRecorder.stop();
        }
        isRecording = false;
        micBtn.classList.remove('recording');
        micBtn.innerText = "🎤";
        updateStatus("Processing...");
    }
}

async function sendAudioToVoice(audioBlob) {
    const formData = new FormData();
    formData.append("file", audioBlob, "recording.webm");
    formData.append("session_id", currentSessionId);
    formData.append("query_history", JSON.stringify(getQueryHistory()));

    // Show Close Button, Hide Stealth Controls
    const closeBtn = document.getElementById('btn-close');
    const stealthControls = document.getElementById('stealth-controls');
    if (closeBtn) closeBtn.style.display = 'block';
    if (stealthControls) stealthControls.style.display = 'none';

    try {
        log("Processing...", "system");
        updateStatus("Processing...");

        const res = await fetch(`${API_BASE}/voice`, {
            method: 'POST',
            body: formData
        });

        const responseData = await res.json();

        // Store learned stop for future speculative fetches
        if (responseData.learned_stop) {
            addToQueryHistory(responseData.learned_stop);
        }

        // Show transcript
        if (responseData.transcript) {
            log(`You: ${responseData.transcript}`, "user");
        }

        const result = responseData.data;

        // Handle Result Types
        if (result.type === "RESULT") {
            const payload = result.payload;
            log(`Agent: ${payload.tts_text}`, "agent");
            updateStatus(payload.tts_text);

            if (payload.vibration) {
                if (navigator.vibrate) navigator.vibrate(payload.vibration);
                document.body.style.backgroundColor = "#ccc";
                setTimeout(() => document.body.style.backgroundColor = "#333", 200);
            }

        } else if (result.type === "CLARIFICATION") {
            const payload = result.payload;
            log(`Agent: ${payload.question_text}`, "agent");
            updateStatus(payload.question_text);
            renderChips(payload.options);

        } else if (result.type === "ERROR") {
            const payload = result.payload;
            log(`Error: ${payload.message}`, "system");
            updateStatus("Error");
        }

        // Play Audio (Async)
        if (responseData.audio_ticket) {
            playAudio(responseData.audio_ticket);
        }

    } catch (e) {
        console.error("Voice query failed:", e);
        log("Error: Voice query failed.", "system");
        updateStatus("Error");
    }
}

function handleInput(e) {
    if (e.key === 'Enter') {
        sendAgentQuery();
    }
}

function log(msg, type = "system") {
    const box = document.getElementById('agent-log');
    if (!box) return;

    const div = document.createElement('div');
    div.className = `log-entry ${type}`;
    div.innerText = msg;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}

function updateStatus(msg) {
    const el = document.getElementById('message');
    if (el) el.innerText = msg;
}

// --- New Agent Logic ---

async function sendAgentQuery(overrideQuery = null) {
    const input = document.getElementById('agent-input');
    const query = overrideQuery || input.value.trim();
    if (!query) return;

    if (!overrideQuery) input.value = "";

    if (!overrideQuery) log(`User: ${query}`, "user"); // Don't log again if it's a chip click
    updateStatus("Thinking...");

    // Show Close Button, Hide Stealth Controls
    const closeBtn = document.getElementById('btn-close');
    const stealthControls = document.getElementById('stealth-controls');
    if (closeBtn) closeBtn.style.display = 'block';
    if (stealthControls) stealthControls.style.display = 'none';

    try {
        const res = await fetch(`${API_BASE}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query: query,
                session_id: currentSessionId,
                query_history: getQueryHistory()
            })
        });
        const responseData = await res.json();

        // Store learned stop for future speculative fetches
        if (responseData.learned_stop) {
            addToQueryHistory(responseData.learned_stop);
        }

        const result = responseData.data;

        // Handle Result Types
        if (result.type === "RESULT") {
            const payload = result.payload;
            log(`Agent: ${payload.tts_text}`, "agent");
            updateStatus(payload.tts_text);

            // Vibration
            if (payload.vibration) {
                if (navigator.vibrate) navigator.vibrate(payload.vibration);
                document.body.style.backgroundColor = "#ccc";
                setTimeout(() => document.body.style.backgroundColor = "#333", 200);
            }

        } else if (result.type === "CLARIFICATION") {
            const payload = result.payload;
            log(`Agent: ${payload.question_text}`, "agent");
            updateStatus(payload.question_text);

            // Render Chips
            renderChips(payload.options);

        } else if (result.type === "ERROR") {
            const payload = result.payload;
            log(`Error: ${payload.message}`, "system");
            updateStatus("Error");
        }

        // Play Audio (Async)
        if (responseData.audio_ticket) {
            playAudio(responseData.audio_ticket);
        }

    } catch (e) {
        console.error(e);
        log(`Error: ${e.message}`, "system");
        updateStatus("Agent Error");
    }
}

function renderChips(options) {
    const box = document.getElementById('agent-log');
    const chipContainer = document.createElement('div');
    chipContainer.className = "chip-container";
    chipContainer.style.marginTop = "10px";

    options.forEach(opt => {
        const chip = document.createElement('button');
        chip.className = "chip";
        chip.innerText = opt.label;
        chip.style.marginRight = "5px";
        chip.style.padding = "5px 10px";
        chip.style.borderRadius = "15px";
        chip.style.border = "none";
        chip.style.background = "#4CAF50";
        chip.style.color = "white";
        chip.style.cursor = "pointer";

        chip.onclick = () => {
            // Remove chips after selection
            chipContainer.remove();
            // Send selection as new query
            log(`Selected: ${opt.label}`, "user");
            sendAgentQuery(opt.value); // Send the value (e.g. "inbound")
        };

        chipContainer.appendChild(chip);
    });

    box.appendChild(chipContainer);
    box.scrollTop = box.scrollHeight;
}

async function playAudio(ticketId) {
    const audioUrl = `${API_BASE}/media/${ticketId}`;
    log("Fetching audio...", "system");

    // Simple retry loop or just let the browser handle the stream?
    // The browser `new Audio(url)` might fail if 202 is returned.
    // We need to fetch() it until 200, then create blob URL.

    try {
        let attempts = 0;
        while (attempts < 10) {
            const res = await fetch(audioUrl);
            if (res.status === 200) {
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                const audio = new Audio(url);
                audio.play().catch(e => console.error("Playback failed", e));
                return;
            } else if (res.status === 202) {
                // Wait and retry
                await new Promise(r => setTimeout(r, 500));
                attempts++;
            } else {
                console.error("Audio fetch failed", res.status);
                return;
            }
        }
    } catch (e) {
        console.error("Audio error", e);
    }
}

async function closeConversation() {
    const logBox = document.getElementById('agent-log');
    if (logBox) logBox.innerHTML = '<div class="log-entry system">System: Ready. Click Mic or type below.</div>';

    const closeBtn = document.getElementById('btn-close');
    const stealthControls = document.getElementById('stealth-controls');

    if (closeBtn) closeBtn.style.display = 'none';
    if (stealthControls) stealthControls.style.display = 'block';

    updateStatus("Ready");
    // Note: session ID persists for query history (speculative fetch)
    // Only the UI/conversation context is cleared
}

// Expose functions to window
window.sendStealth = sendStealth;
window.toggleMic = toggleMic;
window.sendAgentQuery = sendAgentQuery;
window.handleInput = handleInput;
window.closeConversation = closeConversation;

// Initialize
window.addEventListener('load', loadButtonConfig);
