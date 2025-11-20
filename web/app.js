const API_BASE = "/api/v1";

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
        // Shorten name if too long?
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

            // Visual Feedback for Desktop
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
        // Start Recording
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            mediaRecorder = new MediaRecorder(stream);
            audioChunks = [];

            mediaRecorder.ondataavailable = event => {
                audioChunks.push(event.data);
            };

            mediaRecorder.onstop = async () => {
                const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
                await sendAudioToTranscribe(audioBlob);
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
        // Stop Recording
        if (mediaRecorder && mediaRecorder.state !== "inactive") {
            mediaRecorder.stop();
        }
        isRecording = false;
        micBtn.classList.remove('recording');
        micBtn.innerText = "🎤";
        updateStatus("Processing...");
    }
}

async function sendAudioToTranscribe(audioBlob) {
    const formData = new FormData();
    formData.append("file", audioBlob, "recording.webm");

    try {
        log("System: Transcribing...", "system");
        const res = await fetch(`${API_BASE}/transcribe`, {
            method: 'POST',
            body: formData
        });

        const data = await res.json();
        if (data.text) {
            log(`You said: ${data.text}`, "user");
            // Populate input and send
            const input = document.getElementById('agent-input');
            if (input) {
                input.value = data.text;
                sendAgentQuery();
            }
        } else {
            log("System: No speech detected.", "system");
            updateStatus("Ready");
        }
    } catch (e) {
        console.error("Transcription failed:", e);
        log("Error: Transcription failed.", "system");
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


async function sendAgentQuery() {
    const input = document.getElementById('agent-input');
    const query = input.value.trim();
    if (!query) return;

    input.value = "";
    log(`User: ${query}`, "user");
    updateStatus("Thinking...");

    try {
        const res = await fetch(`${API_BASE}/agent`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query })
        });
        const data = await res.json();

        if (data.text) {
            log(`Agent: ${data.text}`, "agent");
            updateStatus(data.text);

            // Handle Configuration Update
            if (data.config_update) {
                const cfg = data.config_update;
                saveButtonConfig(cfg.button_index, cfg);
                log(`Updated Button ${cfg.button_index}: ${cfg.stop_name}`, "system");
            }

            // Handle Vibration
            if (data.vibration && data.vibration.length > 0) {
                log(`Vibrating: ${JSON.stringify(data.vibration)}`, "system");
                if (navigator.vibrate) {
                    navigator.vibrate(data.vibration);
                }
                // Visual flash
                document.body.style.backgroundColor = "#ccc";
                setTimeout(() => document.body.style.backgroundColor = "#333", 200);
            }

            // Handle Audio
            if (data.audio_base64) {
                log("Playing audio...", "system");
                const audio = new Audio("data:audio/wav;base64," + data.audio_base64);
                audio.play().catch(e => console.error("Audio playback failed:", e));
            }

            // Show Close Button, Hide Stealth Controls
            const closeBtn = document.getElementById('btn-close');
            const stealthControls = document.getElementById('stealth-controls');
            if (closeBtn) closeBtn.style.display = 'block';
            if (stealthControls) stealthControls.style.display = 'none';

        } else {
            log("No text received from agent.", "system");
            updateStatus("No response");
        }
    } catch (e) {
        console.error(e);
        log(`Error: ${e.message}`, "system");
        updateStatus("Agent Error");
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

    // Reset Backend Session
    try {
        await fetch(`${API_BASE}/reset`, { method: 'POST' });
    } catch (e) {
        console.error("Failed to reset session:", e);
    }
}

// Expose functions to window
window.sendStealth = sendStealth;
window.toggleMic = toggleMic;
window.sendAgentQuery = sendAgentQuery;
window.handleInput = handleInput;
window.closeConversation = closeConversation;

// Initialize
window.addEventListener('load', loadButtonConfig);
