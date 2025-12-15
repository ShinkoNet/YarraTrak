const API_BASE = "/api/v1";
const SESSION_KEY = "ptv_session_id";
const QUERY_HISTORY_KEY = "ptv_query_history";
const MAX_QUERY_HISTORY = 10;
const WS_MODE_KEY = "ptv_ws_mode";
const API_KEY_KEY = "ptv_api_key";

// Calculate vibration pattern locally (same logic as Pebble app)
// Encodes minutes as haptic pattern: Hours=1000ms, Tens=500ms, Ones=150ms
function calculateVibration(minutes) {
    if (minutes === 0) {
        // "Shave and a haircut" rhythm for NOW!
        return [500, 150, 150, 150, 150, 150, 500, 150, 500, 500, 500, 150, 500];
    }

    minutes = Math.max(0, Math.min(720, minutes));
    const hours = Math.floor(minutes / 60);
    const tens = Math.floor((minutes % 60) / 10);
    const ones = minutes % 10;

    const pattern = [];
    for (let i = 0; i < hours; i++) pattern.push(1000, 400);
    if (hours > 0 && (tens > 0 || ones > 0) && pattern.length) pattern[pattern.length - 1] += 200;

    for (let i = 0; i < tens; i++) pattern.push(500, 300);
    if (tens > 0 && ones > 0 && pattern.length) pattern[pattern.length - 1] += 100;

    for (let i = 0; i < ones; i++) pattern.push(150, 150);

    return pattern;
}

// Get API key from localStorage
function getApiKey() {
    return localStorage.getItem(API_KEY_KEY) || '';
}

// Set API key in localStorage
function setApiKey(key) {
    localStorage.setItem(API_KEY_KEY, key);
}

// Save API key from form input
function saveApiKey() {
    const input = document.getElementById('api-key-input');
    const status = document.getElementById('api-key-status');
    const key = input.value.trim();

    if (key) {
        setApiKey(key);
        input.value = '';
        input.placeholder = 'Key saved';
        if (status) status.textContent = 'Saved';
        setTimeout(() => {
            if (status) status.textContent = '';
            input.placeholder = 'Enter API Key';
        }, 2000);

        // Reconnect WebSocket with new key if WS mode is enabled
        if (useWebSocket) {
            disconnectWebSocket();
            connectWebSocket();
        }
    }
}

// Update API key status indicator on load
function updateApiKeyStatus() {
    const status = document.getElementById('api-key-status');
    const input = document.getElementById('api-key-input');
    if (getApiKey()) {
        if (status) status.textContent = 'Key set';
        if (input) input.placeholder = 'Update API Key';
    }
}

// Get headers with optional API key
function getApiHeaders(extraHeaders = {}) {
    const headers = { 'Content-Type': 'application/json', ...extraHeaders };
    const apiKey = getApiKey();
    if (apiKey) {
        headers['X-API-Key'] = apiKey;
    }
    return headers;
}

// Generate UUID - fallback for non-secure contexts (http://)
function generateUUID() {
    if (crypto.randomUUID) {
        return crypto.randomUUID();
    }
    // Fallback for HTTP contexts
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        const r = Math.random() * 16 | 0;
        const v = c === 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
}

let currentSessionId = localStorage.getItem(SESSION_KEY) || generateUUID();
localStorage.setItem(SESSION_KEY, currentSessionId);

// --- WebSocket State ---
let ws = null;
let wsConnected = false;
let useWebSocket = localStorage.getItem(WS_MODE_KEY) === "true";
let pendingRequests = new Map(); // id -> {resolve, reject}
let wsMessageId = 0;

// --- WebSocket Connection ---
function connectWebSocket() {
    if (ws && ws.readyState === WebSocket.OPEN) return;

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let wsUrl = `${protocol}//${location.host}/ws`;

    // Build query params
    const params = [];

    // Add API key if set
    const apiKey = getApiKey();
    if (apiKey) {
        params.push(`api_key=${encodeURIComponent(apiKey)}`);
    }

    // Build buttons query param for instant data on connect
    // Format: "1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID"
    const buttonParts = [];
    for (let i = 1; i <= 3; i++) {
        const config = getButtonConfig(i);
        if (config && config.stop_id) {
            const routeType = config.route_type || 0;
            let part = `${i}:${config.stop_id}:${routeType}`;
            if (config.direction_id !== undefined && config.direction_id !== null) {
                part += `:${config.direction_id}`;
            }
            buttonParts.push(part);
        }
    }
    if (buttonParts.length > 0) {
        params.push(`buttons=${encodeURIComponent(buttonParts.join(','))}`);
    }

    if (params.length > 0) {
        wsUrl += '?' + params.join('&');
    }

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        wsConnected = true;
        log("WebSocket connected", "system");
        updateWsStatus();
        // Note: server will push stealth_update immediately if buttons were in URL
        // Still call sendStealthSubscription for mid-session updates
        sendStealthSubscription();
    };

    ws.onclose = () => {
        wsConnected = false;
        log("WebSocket disconnected", "system");
        updateWsStatus();
        // Auto-reconnect after 2 seconds if WS mode is still enabled
        if (useWebSocket) {
            setTimeout(connectWebSocket, 2000);
        }
    };

    ws.onerror = (e) => {
        console.error("WebSocket error:", e);
        log("WebSocket error", "system");
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);

            // Handle live stealth updates (broadcast, no pending request)
            if (msg.type === 'stealth_update') {
                updateButtonSubtitles(msg.updates || []);
                return;
            }

            const pending = pendingRequests.get(msg.id);
            if (pending) {
                pendingRequests.delete(msg.id);
                // Handle button config push from server
                if (msg.button_config) {
                    saveButtonConfigFromServer(msg.button_config);
                }
                pending.resolve(msg);
            }
        } catch (e) {
            console.error("WebSocket message parse error:", e);
        }
    };
}

function disconnectWebSocket() {
    if (ws) {
        ws.close();
        ws = null;
    }
    wsConnected = false;
}

function toggleWsMode() {
    useWebSocket = !useWebSocket;
    localStorage.setItem(WS_MODE_KEY, useWebSocket);
    updateWsStatus();

    if (useWebSocket) {
        connectWebSocket();
    } else {
        disconnectWebSocket();
    }
}

function updateWsStatus() {
    const btn = document.getElementById('ws-toggle');
    if (btn) {
        btn.innerText = useWebSocket ? (wsConnected ? '🔌 WS: ON' : '🔌 WS: ...') : '🔌 WS: OFF';
        btn.title = useWebSocket ? 'Click to use HTTP' : 'Click to use WebSocket';
    }
}

async function sendWsQuery(query) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        throw new Error("WebSocket not connected");
    }

    const id = String(++wsMessageId);

    return new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
            pendingRequests.delete(id);
            reject(new Error("Request timeout"));
        }, 30000);

        pendingRequests.set(id, {
            resolve: (msg) => {
                clearTimeout(timeout);
                resolve(msg);
            },
            reject: (err) => {
                clearTimeout(timeout);
                reject(err);
            }
        });

        ws.send(JSON.stringify({
            type: "query",
            id: id,
            text: query,
            session_id: currentSessionId,
            query_history: getQueryHistory()
        }));
    });
}

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

// Handle button config pushed from server (via LLM/agent)
function saveButtonConfigFromServer(config) {
    if (!config || !config.button_id) return;

    const btnId = config.button_id;
    // Map 'name' from server to 'stop_name' which updateButtonUI expects
    const stopName = config.name || config.stop_name || `Button ${btnId}`;
    const btnConfig = {
        stop_name: stopName,
        stop_id: config.stop_id,
        route_type: config.route_type || 0,
        direction_id: config.direction_id,
        direction_name: config.direction_name
    };

    log(`Button ${btnId} configured: ${stopName} (Stop ${config.stop_id})`, "system");
    saveButtonConfig(btnId, btnConfig);

    // Re-subscribe to get live updates for the new button
    sendStealthSubscription();
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
        const title = btn.querySelector('.btn-title');
        const subtitle = btn.querySelector('.btn-subtitle');
        let name = config.stop_name || `Button ${index}`;
        if (name.length > 15) name = name.substring(0, 13) + "..";
        if (title) title.textContent = name;
        if (subtitle) subtitle.textContent = 'Waiting...';
        btn.title = config.direction_name ? `${config.stop_name} -> ${config.direction_name}` : config.stop_name;
    }
}

// Subscribe to live stealth updates
function sendStealthSubscription() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const buttons = [];
    for (let i = 1; i <= 3; i++) {
        const config = getButtonConfig(i);
        if (config && config.stop_id) {
            buttons.push({
                button_id: i,
                stop_id: config.stop_id,
                route_type: config.route_type || 0,
                direction_id: config.direction_id
            });
        }
    }

    if (buttons.length > 0) {
        log(`Subscribing to ${buttons.length} stealth buttons`, "system");
        ws.send(JSON.stringify({
            type: 'subscribe_stealth',
            buttons: buttons
        }));
    }
}

// Update button subtitles from live updates
function updateButtonSubtitles(updates) {
    updates.forEach(u => {
        const btn = document.getElementById(`btn-${u.button_id}`);
        if (btn) {
            const subtitle = btn.querySelector('.btn-subtitle');
            if (subtitle) {
                subtitle.textContent = u.message || '--';
            }
        }
    });
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
            headers: getApiHeaders(),
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

        // Server no longer sends vibration - we calculate locally
        log(data.message || "Done", "system");
        updateStatus(data.message || "Done");

        // Visual Feedback
        document.body.style.backgroundColor = "#555";
        setTimeout(() => document.body.style.backgroundColor = "#333", 200);

        // Calculate vibration pattern from message (extract minutes)
        // Message format: "X min" or "Arriving Now" or "Now"
        if (navigator.vibrate) {
            let minutes = 0;
            const match = data.message && data.message.match(/(\d+)\s*min/);
            if (match) {
                minutes = parseInt(match[1], 10);
            }
            navigator.vibrate(calculateVibration(minutes));
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

    // Show Close Button, Hide Stealth Controls and previous clarify options
    const closeBtn = document.getElementById('btn-close');
    const stealthControls = document.getElementById('stealth-controls');
    if (closeBtn) closeBtn.style.display = 'block';
    if (stealthControls) stealthControls.style.display = 'none';
    clearClarifyOptions();

    try {
        log("Processing...", "system");
        updateStatus("Processing...");

        const res = await fetch(`${API_BASE}/voice`, {
            method: 'POST',
            headers: (() => {
                const h = {};
                const apiKey = getApiKey();
                if (apiKey) h['X-API-Key'] = apiKey;
                return h;
            })(),
            body: formData
        });

        if (res.status === 401) {
            log("Error: Invalid or missing API key. Please enter your API key above.", "system");
            updateStatus("API Key Required");
            return;
        }

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

            // Calculate vibration locally from departure time
            const departure = payload.departure;
            if (departure && departure.minutes_to_depart !== undefined) {
                if (navigator.vibrate) navigator.vibrate(calculateVibration(departure.minutes_to_depart));
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

    // Show Close Button, Hide Stealth Controls and previous clarify options
    const closeBtn = document.getElementById('btn-close');
    const stealthControls = document.getElementById('stealth-controls');
    if (closeBtn) closeBtn.style.display = 'block';
    if (stealthControls) stealthControls.style.display = 'none';
    clearClarifyOptions();

    try {
        let responseData;

        // Use WebSocket if enabled and connected
        if (useWebSocket && wsConnected) {
            const wsResponse = await sendWsQuery(query);

            if (wsResponse.type === "error") {
                throw new Error(wsResponse.error || "WebSocket query failed");
            }

            responseData = {
                data: wsResponse.data,
                learned_stop: wsResponse.learned_stop,
                audio_ticket: null  // WebSocket doesn't return audio tickets
            };
        } else {
            // Fall back to HTTP
            const res = await fetch(`${API_BASE}/query`, {
                method: 'POST',
                headers: getApiHeaders(),
                body: JSON.stringify({
                    query: query,
                    session_id: currentSessionId,
                    query_history: getQueryHistory()
                })
            });

            if (res.status === 401) {
                log("Error: Invalid or missing API key. Please enter your API key above.", "system");
                updateStatus("API Key Required");
                return;
            }

            responseData = await res.json();
        }

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

            // Calculate vibration locally from departure time
            const departure = payload.departure;
            if (departure && departure.minutes_to_depart !== undefined) {
                if (navigator.vibrate) navigator.vibrate(calculateVibration(departure.minutes_to_depart));
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

        // Play Audio (Async) - only for HTTP mode
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
    // Render in Pebble screen (clarify-options container)
    const clarifyContainer = document.getElementById('clarify-options');
    clarifyContainer.innerHTML = '';
    clarifyContainer.style.display = 'flex';

    options.forEach(opt => {
        const btn = document.createElement('button');
        btn.className = "clarify-btn";
        btn.innerText = opt.label;

        btn.onclick = () => {
            // Hide options after selection
            clearClarifyOptions();
            // Send selection as new query
            log(`Selected: ${opt.label}`, "user");
            sendAgentQuery(opt.value);
        };

        clarifyContainer.appendChild(btn);
    });

    // Also render in debug console for reference
    const box = document.getElementById('agent-log');
    const chipContainer = document.createElement('div');
    chipContainer.className = "chip-container";

    options.forEach(opt => {
        const chip = document.createElement('button');
        chip.className = "chip";
        chip.innerText = opt.label;

        chip.onclick = () => {
            chipContainer.remove();
            clearClarifyOptions();
            log(`Selected: ${opt.label}`, "user");
            sendAgentQuery(opt.value);
        };

        chipContainer.appendChild(chip);
    });

    box.appendChild(chipContainer);
    box.scrollTop = box.scrollHeight;
}

function clearClarifyOptions() {
    const clarifyContainer = document.getElementById('clarify-options');
    if (clarifyContainer) {
        clarifyContainer.innerHTML = '';
        clarifyContainer.style.display = 'none';
    }
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

    clearClarifyOptions();
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
window.toggleWsMode = toggleWsMode;
window.saveApiKey = saveApiKey;

// Initialize
window.addEventListener('load', () => {
    loadButtonConfig();
    updateWsStatus();
    updateApiKeyStatus();

    // Connect WebSocket if enabled
    if (useWebSocket) {
        connectWebSocket();
    }
});

