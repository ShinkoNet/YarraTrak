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

// Initialize settings
Settings.config({
    url: CONFIG_URL
},
    function (e) {
        console.log('Config opened');
    },
    function (e) {
        console.log('Config closed with:', JSON.stringify(e.options));
        if (e.options && e.options.server_url) {
            reconnect();
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

    // Voice option (if microphone available)
    if (Feature.microphone()) {
        items.push({
            title: 'Ask',
            subtitle: 'Voice query'
        });
    }

    // Stealth buttons from settings
    var btn1Name = Settings.option('stealth_1_name');
    var btn2Name = Settings.option('stealth_2_name');
    var btn3Name = Settings.option('stealth_3_name');

    if (btn1Name) {
        items.push({ title: btn1Name, subtitle: 'Quick check', data: { stealth: 1 } });
    }
    if (btn2Name) {
        items.push({ title: btn2Name, subtitle: 'Quick check', data: { stealth: 2 } });
    }
    if (btn3Name) {
        items.push({ title: btn3Name, subtitle: 'Quick check', data: { stealth: 3 } });
    }

    // Fallback
    if (items.length === 0) {
        items.push({
            title: 'Setup Required',
            subtitle: 'Open settings'
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

// Stealth query
function runStealthQuery(buttonIndex) {
    var query = Settings.option('stealth_' + buttonIndex + '_query');
    var name = Settings.option('stealth_' + buttonIndex + '_name');

    if (!query) {
        Vibe.vibrate('short');
        return;
    }

    loadingCard.title(name || 'Checking...');
    loadingCard.subtitle('');
    loadingCard.body('');
    loadingCard.show();

    sendQuery(query, function (response) {
        loadingCard.hide();

        var payload = response.data && response.data.payload;
        if (payload && payload.vibration) {
            Vibe.vibrate(payload.vibration);
        } else {
            Vibe.vibrate('short');
        }
    }, function (error) {
        loadingCard.hide();
        Vibe.vibrate('double');
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
            Vibe.vibrate(payload.vibration);
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
    var serverUrl = Settings.option('server_url');

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

    ws = new WebSocket(wsUrl);

    ws.onopen = function () {
        wsConnected = true;
        console.log('WebSocket connected');
        loadingCard.subtitle('Connected!');
        setTimeout(function () {
            loadingCard.hide();
            mainMenu.show();
        }, 500);
    };

    ws.onclose = function () {
        wsConnected = false;
        console.log('WebSocket closed');
        // Auto-reconnect
        setTimeout(connectWebSocket, 3000);
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
    if (ws) {
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