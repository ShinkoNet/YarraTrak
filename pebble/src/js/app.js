/**
 * PTV Notify - Melbourne Public Transport for Pebble
 * 
 * Main application entry point.
 * Runs on the phone's PebbleKit JS runtime.
 */

var UI = require('ui');
var Voice = require('ui/voice');
var Settings = require('settings');
var Vibe = require('ui/vibe');
var Feature = require('platform/feature');
var PTVWS = require('ptv-ws');

// Configuration URL - you'll need to host this
var CONFIG_URL = 'https://your-server.com/pebble-config.html';

// Application state
var wsClient = null;
var sessionId = null;
var queryHistory = [];

// Initialize settings
Settings.config({
    url: CONFIG_URL
},
    function (e) {
        console.log('Config opened');
    },
    function (e) {
        console.log('Config closed with:', e.options);
        // Reconnect with new settings
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
            subtitle: 'Voice query',
            icon: null
        });
    }

    // Stealth buttons
    var btn1 = Settings.option('stealth_1_name');
    var btn2 = Settings.option('stealth_2_name');
    var btn3 = Settings.option('stealth_3_name');

    if (btn1) {
        items.push({ title: btn1, subtitle: 'Quick check', data: { stealth: 1 } });
    }
    if (btn2) {
        items.push({ title: btn2, subtitle: 'Quick check', data: { stealth: 2 } });
    }
    if (btn3) {
        items.push({ title: btn3, subtitle: 'Quick check', data: { stealth: 3 } });
    }

    // If no items, show placeholder
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

mainMenu.on('longSelect', function (e) {
    if (e.item.data && e.item.data.stealth) {
        // Long press shows last result again
        Vibe.vibrate('short');
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
            resultCard.body('Dictation failed: ' + e.err);
            return;
        }

        // Got transcription
        resultCard.title('Processing...');
        resultCard.body('You said: ' + e.transcription);

        // Send to server
        sendQuery(e.transcription, function (response) {
            handleQueryResponse(resultCard, response);
        }, function (error) {
            resultCard.title('Error');
            resultCard.body(error.message || 'Query failed');
        });
    });

    // Allow re-asking
    resultCard.on('click', 'select', function () {
        startVoiceQuery();
    });

    resultCard.on('click', 'back', function () {
        resultCard.hide();
    });
}

// Stealth query (pre-configured)
function runStealthQuery(buttonIndex) {
    var stopName = Settings.option('stealth_' + buttonIndex + '_name');
    var query = Settings.option('stealth_' + buttonIndex + '_query');

    if (!query) {
        Vibe.vibrate('short');
        return;
    }

    // Show brief feedback
    loadingCard.title(stopName || 'Checking...');
    loadingCard.subtitle('');
    loadingCard.body('');
    loadingCard.show();

    sendQuery(query, function (response) {
        loadingCard.hide();

        // Vibrate the pattern
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

// Send query via WebSocket
function sendQuery(text, successCb, errorCb) {
    if (!wsClient || !wsClient.isConnected()) {
        errorCb({ message: 'Not connected' });
        return;
    }

    wsClient.query(text, sessionId, queryHistory, function (response) {
        // Store learned stop
        if (response.learned_stop) {
            addToHistory(response.learned_stop);
        }
        // Update session
        if (response.session_id) {
            sessionId = response.session_id;
        }
        successCb(response);
    }, errorCb);
}

// Handle query response
function handleQueryResponse(card, response) {
    var data = response.data;

    if (!data) {
        card.title('Error');
        card.body('No response data');
        return;
    }

    if (data.type === 'RESULT') {
        var payload = data.payload;
        card.title('Departures');
        card.body(payload.tts_text || 'No information');

        // Vibrate
        if (payload.vibration) {
            Vibe.vibrate(payload.vibration);
        }

    } else if (data.type === 'CLARIFICATION') {
        var payload = data.payload;
        card.title('Choose');
        card.body(payload.question_text || 'Please clarify');

        // Show clarification menu
        showClarificationMenu(payload.options || [], card);

    } else if (data.type === 'ERROR') {
        var payload = data.payload;
        card.title('Error');
        card.body(payload.message || 'Unknown error');
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
            parentCard.body(error.message || 'Query failed');
        });
    });

    menu.on('back', function () {
        menu.hide();
    });

    menu.show();
}

// Query history management
function addToHistory(stopInfo) {
    if (!stopInfo || !stopInfo.stop_id) return;

    // Remove duplicate
    queryHistory = queryHistory.filter(function (h) {
        return !(h.stop_id === stopInfo.stop_id && h.route_type === stopInfo.route_type);
    });

    // Add to front
    queryHistory.unshift(stopInfo);

    // Limit size
    if (queryHistory.length > 5) {
        queryHistory = queryHistory.slice(0, 5);
    }
}

// Connection management
function connect() {
    var serverUrl = Settings.option('server_url');

    if (!serverUrl) {
        loadingCard.title('Setup Required');
        loadingCard.subtitle('');
        loadingCard.body('Open settings in the\nPebble app to configure\nthe server URL.');
        loadingCard.show();
        return;
    }

    loadingCard.title('PTV Notify');
    loadingCard.subtitle('Connecting...');
    loadingCard.body('');
    loadingCard.show();

    // Create WebSocket client
    wsClient = new PTVWS(serverUrl);

    wsClient.on('open', function () {
        loadingCard.subtitle('Connected!');
        setTimeout(function () {
            loadingCard.hide();
            mainMenu.show();
        }, 500);
    });

    wsClient.on('close', function () {
        console.log('WebSocket closed');
    });

    wsClient.on('error', function (e) {
        loadingCard.title('Connection Failed');
        loadingCard.subtitle('');
        loadingCard.body('Could not connect to:\n' + serverUrl);
    });

    wsClient.connect();
}

function reconnect() {
    if (wsClient) {
        wsClient.disconnect();
        wsClient = null;
    }
    connect();
}

// Generate session ID
function generateSessionId() {
    var chars = 'abcdef0123456789';
    var id = '';
    for (var i = 0; i < 16; i++) {
        id += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return id;
}

// Start the app
sessionId = generateSessionId();
connect();
