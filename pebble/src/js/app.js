/**
 * PTV Notify - Melbourne Public Transport for Pebble
 * 
 * Voice-enabled departure queries via WebSocket.
 */

var UI = require('ui');
var Voice = require('ui/voice');
var Settings = require('settings');
var Vibe = require('ui/vibe');
var Feature = require('platform/feature');

// Custom vibration pattern player
// Pebble.js only supports 'short', 'long', 'double' - we simulate patterns
// Pattern format: [vibe_ms, pause_ms, vibe_ms, pause_ms, ...]
var vibeTimer = null;

function playVibrationPattern(pattern) {
    // Clear any ongoing pattern
    if (vibeTimer) {
        clearTimeout(vibeTimer);
        vibeTimer = null;
    }

    // If it's a string preset, just use it directly
    if (typeof pattern === 'string') {
        Vibe.vibrate(pattern);
        return;
    }

    // If not an array or empty, do a short vibe
    if (!Array.isArray(pattern) || pattern.length === 0) {
        Vibe.vibrate('short');
        return;
    }

    var index = 0;

    function playNext() {
        if (index >= pattern.length) {
            vibeTimer = null;
            return;
        }

        var duration = pattern[index];
        var isVibration = (index % 2 === 0); // Even indices are vibrations

        if (isVibration && duration > 0) {
            // Map duration to preset: <250ms = short, else long
            // short ~50ms, long ~500ms on Pebble
            if (duration < 250) {
                Vibe.vibrate('short');
            } else {
                Vibe.vibrate('long');
            }
        }

        index++;

        // Schedule next step after this duration
        if (index < pattern.length) {
            vibeTimer = setTimeout(playNext, duration);
        }
    }

    playNext();
}
var Vector2 = require('vector2');

// Server configuration - update this URL to your server
var CONFIG_URL = 'http://10.1.0.88:8000/pebble-config.html';

// Application state

var ws = null;
var wsConnected = false;
var sessionId = generateUUID();
var queryHistory = [];
var messageId = 0;
var pendingRequests = {};

// Generate simple UUID
function generateUUID() {
    var chars = 'abcdef0123456789';
    var id = '';
    for (var i = 0; i < 32; i++) {
        id += chars.charAt(Math.floor(Math.random() * chars.length));
        if (i === 7 || i === 11 || i === 15 || i === 19) id += '-';
    }
    return id;
}

// Initialize settings with defaults for emulator testing
var DEFAULT_SERVER_URL = 'http://10.1.0.88:8000';

// Set default if not already configured
if (!Settings.option('server_url')) {
    Settings.option('server_url', DEFAULT_SERVER_URL);
}

// Track active URL to prevent duplicate connections
var activeServerUrl = Settings.option('server_url');

Settings.config({
    url: CONFIG_URL
},
    function (e) {
        console.log('Config opened');
    },
    function (e) {
        console.log('Config closed with:', JSON.stringify(e.options));

        // Always refresh menu items to show new button config immediately
        mainMenu.items(0, buildMenuItems());

        if (e.options && e.options.server_url) {
            var newUrl = e.options.server_url;
            if (newUrl !== activeServerUrl) {
                console.log('Server URL changed from ' + activeServerUrl + ' to ' + newUrl);
                activeServerUrl = newUrl;
                reconnect();
            } else {
                console.log('Server URL unchanged, skipping reconnect');
                // If not reconnecting, make sure we are connected
                if (!wsConnected) {
                    connectWebSocket();
                }
            }
        }
    }
);

// Loading screen
var loadingCard = new UI.Card({
    title: 'PTV Notify',
    subtitle: 'Starting...',
    scrollable: false
});

// Main menu
var mainMenu = new UI.Menu({
    backgroundColor: Feature.color('black', 'black'),
    textColor: Feature.color('white', 'white'),
    highlightBackgroundColor: Feature.color('vivid-cerulean', 'white'),
    highlightTextColor: Feature.color('black', 'black'),
    sections: [{
        title: 'PTV Notify'
    }]
});

// Build menu items
function buildMenuItems() {
    var items = [];

    // Smart station name abbreviation for Aplite menu display
    // Uses recognizable short forms instead of just truncating
    function abbreviateStation(name, maxLen) {
        if (!name) return '';
        maxLen = maxLen || 16; // Aplite safe width

        // Remove " Station" suffix
        name = name.replace(/ Station$/i, '');

        // Common abbreviations for Melbourne stations
        var abbrevs = {
            'Flinders Street': 'City',
            'Southern Cross': 'S Cross',
            'South Morang': 'S Morang',
            'South Yarra': 'S Yarra',
            'South Kensington': 'S Kensi',
            'Melbourne Central': 'M Cntral',
            'North Melbourne': 'N Melb',
            'North Richmond': 'N Rchmnd',
            'East Richmond': 'E Rchmnd',
            'West Richmond': 'W Rchmnd',
            'East Malvern': 'E Mlvn',
            'East Camberwell': 'E Camb',
            'Mount Waverley': 'Mt Wav',
            'Narre Warren': 'Narre',
            'Flagstaff': 'Flgstf',
            'Parliament': 'Prlmnt'
        };

        if (abbrevs[name]) return abbrevs[name];

        // Multi-word names: ALWAYS abbreviate (2 chars + 5 chars)
        var words = name.split(' ');
        if (words.length > 1) {
            return words[0].substring(0, 2) + ' ' + words[1].substring(0, 5);
        }

        // Single word - max 7 chars
        return name.substring(0, 7);
    }

    // Voice option only if microphone available (aplite doesn't have mic)
    if (Feature.microphone()) {
        items.push({
            title: 'Ask',
            subtitle: 'Voice query'
        });
    }

    // Stealth buttons from settings - show as "Start→Dest"
    for (var i = 1; i <= 3; i++) {
        var startName = Settings.option('btn' + i + '_name');
        var destName = Settings.option('btn' + i + '_dest_name');
        if (startName) {
            var title = abbreviateStation(startName) + '>' + abbreviateStation(destName || '?');
            items.push({ title: title, subtitle: 'Quick check', data: { stealth: i } });
        }
    }

    // If no items at all, show setup message
    if (items.length === 0) {
        items.push({
            title: 'No buttons set',
            subtitle: 'Configure in phone app'
        });
    }

    return items;
}

// Menu handlers
mainMenu.on('show', function () {
    mainMenu.items(0, buildMenuItems());
});

mainMenu.on('select', function (e) {
    if (e.item.title === 'Ask') {
        startVoiceQuery();
    } else if (e.item.data && e.item.data.stealth) {
        runStealthQuery(e.item.data.stealth);
    }
});

// Voice query flow
function startVoiceQuery() {
    var resultCard = new UI.Card({
        title: 'Listening...',
        scrollable: true
    });
    resultCard.show();

    Voice.dictate('start', true, function (e) {
        if (e.err) {
            if (e.err === 'systemAborted') {
                resultCard.hide();
                return;
            }
            resultCard.title('Error');
            resultCard.body('Dictation: ' + e.err);
            return;
        }

        resultCard.title('Processing...');
        resultCard.body('You: ' + e.transcription);

        sendQuery(e.transcription, function (response) {
            handleQueryResponse(resultCard, response);
        }, function (error) {
            resultCard.title('Error');
            resultCard.body(error.message || 'Query failed');
        });
    });

    resultCard.on('click', 'select', function () {
        startVoiceQuery();
    });

    resultCard.on('click', 'back', function () {
        resultCard.hide();
    });
}

// Stealth query - uses direct stop_id lookup instead of text query for speed
function runStealthQuery(buttonIndex) {
    var name = Settings.option('btn' + buttonIndex + '_name');
    var stopId = Settings.option('btn' + buttonIndex + '_stop_id');
    var routeType = Settings.option('btn' + buttonIndex + '_route_type');
    var directionId = Settings.option('btn' + buttonIndex + '_direction_id');

    if (!stopId) {
        Vibe.vibrate('short');
        return;
    }

    loadingCard.title(name || 'Checking...');
    loadingCard.subtitle('');
    loadingCard.body('');
    loadingCard.show();

    sendStealthQuery(stopId, routeType, directionId, function (response) {
        // Show the result message before hiding
        if (response.message) {
            loadingCard.title(name || 'Result');
            loadingCard.body(response.message);
        }

        if (response.vibration) {
            playVibrationPattern(response.vibration);
        } else {
            Vibe.vibrate('short');
        }

        // Auto-hide after 3 seconds so user can see the message
        setTimeout(function () {
            loadingCard.hide();
        }, 3000);
    }, function (error) {
        loadingCard.title('Error');
        loadingCard.body(error || 'Connection failed');
        Vibe.vibrate('double');

        setTimeout(function () {
            loadingCard.hide();
        }, 2000);
    });
}

// Handle query response
function handleQueryResponse(card, response) {
    var data = response.data;

    if (!data) {
        card.title('Error');
        card.body('No response');
        return;
    }

    if (data.type === 'RESULT') {
        var payload = data.payload;
        card.title('Departures');
        card.body(payload.tts_text || 'No info');

        if (payload.vibration) {
            playVibrationPattern(payload.vibration);
        }
    } else if (data.type === 'CLARIFICATION') {
        var payload = data.payload;
        card.title('Choose');
        card.body(payload.question_text || 'Please clarify');
        showClarificationMenu(payload.options || [], card);
    } else if (data.type === 'ERROR') {
        card.title('Error');
        card.body(data.payload.message || 'Unknown error');
    }
}

// Clarification menu
function showClarificationMenu(options, parentCard) {
    if (!options || options.length === 0) return;

    var menu = new UI.Menu({
        sections: [{
            title: 'Choose One',
            items: options.map(function (opt) {
                return {
                    title: opt.label,
                    data: { value: opt.value }
                };
            })
        }]
    });

    menu.on('select', function (e) {
        menu.hide();
        parentCard.title('Processing...');
        parentCard.body('');

        sendQuery(e.item.data.value, function (response) {
            handleQueryResponse(parentCard, response);
        }, function (error) {
            parentCard.title('Error');
            parentCard.body(error.message || 'Failed');
        });
    });

    menu.on('back', function () {
        menu.hide();
    });

    menu.show();
}

// WebSocket connection
function connectWebSocket() {
    var serverUrl = Settings.option('server_url') || DEFAULT_SERVER_URL;

    console.log('Server URL from settings: ' + Settings.option('server_url'));
    console.log('Using server URL: ' + serverUrl);

    if (!serverUrl) {
        loadingCard.title('Setup Required');
        loadingCard.subtitle('');
        loadingCard.body('Open settings in\nthe Pebble app');
        loadingCard.show();
        return;
    }

    // Convert to WebSocket URL
    var wsUrl = serverUrl
        .replace('https://', 'wss://')
        .replace('http://', 'ws://')
        .replace(/\/+$/, '') + '/ws';

    console.log('Connecting to: ' + wsUrl);

    loadingCard.title('PTV Notify');
    loadingCard.subtitle('Connecting...');
    loadingCard.body('');
    loadingCard.show();

    // Defensive cleanup
    if (ws) {
        ws.onclose = function () { };
        ws.close();
        ws = null;
    }

    ws = new WebSocket(wsUrl);

    var isFirstLoad = true;

    ws.onopen = function () {
        wsConnected = true;
        console.log('WebSocket connected');
        loadingCard.subtitle('Connected!');

        // Refresh menu items immediately when connected
        mainMenu.items(0, buildMenuItems());

        setTimeout(function () {
            loadingCard.hide();
            if (isFirstLoad) {
                mainMenu.show();
                isFirstLoad = false;
            }
        }, 500);
    };

    ws.onclose = function () {
        wsConnected = false;
        console.log('WebSocket closed');
        // Auto-reconnect
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = function (e) {
        console.log('WebSocket error');
        loadingCard.title('Connection Failed');
        loadingCard.body(serverUrl);
    };

    ws.onmessage = function (event) {
        try {
            var msg = JSON.parse(event.data);
            var pending = pendingRequests[msg.id];
            if (pending) {
                delete pendingRequests[msg.id];
                if (pending.timeout) clearTimeout(pending.timeout);

                if (msg.type === 'error') {
                    pending.errorCb({ message: msg.error });
                } else {
                    if (msg.learned_stop) {
                        addToHistory(msg.learned_stop);
                    }
                    if (msg.session_id) {
                        sessionId = msg.session_id;
                    }
                    // Handle button config push from server
                    if (msg.button_config) {
                        saveButtonConfig(msg.button_config);
                    }
                    pending.successCb(msg);
                }
            }
        } catch (e) {
            console.log('Parse error: ' + e);
        }
    };
}

function reconnect() {
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws) {
        ws.onclose = function () { };
        ws.close();
        ws = null;
    }
    wsConnected = false;
    connectWebSocket();
}

// Send query
function sendQuery(text, successCb, errorCb) {
    if (!ws || !wsConnected) {
        errorCb({ message: 'Not connected' });
        return;
    }

    var id = String(++messageId);

    pendingRequests[id] = {
        successCb: successCb,
        errorCb: errorCb,
        timeout: setTimeout(function () {
            delete pendingRequests[id];
            errorCb({ message: 'Timeout' });
        }, 30000)
    };

    ws.send(JSON.stringify({
        type: 'query',
        id: id,
        text: text,
        session_id: sessionId,
        query_history: queryHistory
    }));
}

// Send stealth query - direct stop_id lookup, no LLM
function sendStealthQuery(stopId, routeType, directionId, successCb, errorCb) {
    if (!ws || !wsConnected) {
        errorCb({ message: 'Not connected' });
        return;
    }

    var id = String(++messageId);

    pendingRequests[id] = {
        successCb: successCb,
        errorCb: errorCb,
        timeout: setTimeout(function () {
            delete pendingRequests[id];
            errorCb({ message: 'Timeout' });
        }, 15000)
    };

    var msg = {
        type: 'stealth',
        id: id,
        stop_id: stopId,
        route_type: routeType || 0
    };
    if (directionId !== undefined && directionId !== null) {
        msg.direction_id = directionId;
    }

    ws.send(JSON.stringify(msg));
}

// Query history
function addToHistory(stopInfo) {
    if (!stopInfo || !stopInfo.stop_id) return;

    queryHistory = queryHistory.filter(function (h) {
        return !(h.stop_id === stopInfo.stop_id && h.route_type === stopInfo.route_type);
    });

    queryHistory.unshift(stopInfo);

    if (queryHistory.length > 5) {
        queryHistory = queryHistory.slice(0, 5);
    }
}

// Save button config from server
function saveButtonConfig(config) {
    if (!config || !config.button_id) return;

    var btnId = config.button_id;
    console.log('Saving button ' + btnId + ' config: ' + JSON.stringify(config));

    Settings.option('btn' + btnId + '_name', config.name || ('Button ' + btnId));
    Settings.option('btn' + btnId + '_stop_id', config.stop_id);
    Settings.option('btn' + btnId + '_route_type', config.route_type || 0);

    if (config.direction_id !== undefined && config.direction_id !== null) {
        Settings.option('btn' + btnId + '_direction_id', config.direction_id);
    }

    // Vibrate to confirm
    Vibe.vibrate('short');
    console.log('Button ' + btnId + ' saved successfully');
}

// Start the app
console.log('PTV Notify starting...');
connectWebSocket();