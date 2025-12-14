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
var CONFIG_URL = 'https://ptv.netcavy.net/pebble-config.html';

// Application state

var ws = null;
var wsConnected = false;
var sessionId = generateUUID();
var queryHistory = [];
var messageId = 0;
var pendingRequests = {};

// Live departure data from server broadcasts: {1: {minutes: 3, platform: "2", message: "3 min • P2"}, ...}
var buttonDepartures = {};

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

// Initialize settings with defaults
var DEFAULT_SERVER_URL = 'https://ptv.netcavy.net';

// Hardcoded server URL - always use DEFAULT_SERVER_URL

Settings.config({
    url: CONFIG_URL
},
    function (e) {
        console.log('Config opened');
    },
    function (e) {
        console.log('Config closed with:', JSON.stringify(e.options));

        // Clear stale departure cache - button configs may have changed
        buttonDepartures = {};

        // Always refresh menu items to show new button config immediately
        mainMenu.items(0, buildMenuItems());

        // Reconnect WebSocket with new buttons in URL to get fresh data
        // This ensures only ONE subscription mechanism (URL params) is used
        reconnect();
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

    // Stealth buttons from settings - show as "Start→Dest" with live departure time
    for (var i = 1; i <= 3; i++) {
        var startName = Settings.option('btn' + i + '_name');
        var destName = Settings.option('btn' + i + '_dest_name');
        if (startName) {
            var title = abbreviateStation(startName) + '>' + abbreviateStation(destName || '?');
            // Use live departure data if available, otherwise show "Waiting..."
            var dep = buttonDepartures[i];
            var subtitle = dep ? dep.message : 'Waiting...';
            items.push({ title: title, subtitle: subtitle, data: { stealth: i } });
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

// Countdown timer for live seconds display
var countdownTimer = null;

// Stealth query - uses cached live departure data, no server call needed
function runStealthQuery(buttonIndex) {
    var name = Settings.option('btn' + buttonIndex + '_name');
    var destName = Settings.option('btn' + buttonIndex + '_dest_name');
    var stopId = Settings.option('btn' + buttonIndex + '_stop_id');

    // Clear any existing countdown
    if (countdownTimer) {
        clearInterval(countdownTimer);
        countdownTimer = null;
    }

    if (!stopId) {
        Vibe.vibrate('short');
        return;
    }

    // Use cached live departure data
    var dep = buttonDepartures[buttonIndex];

    // Abbreviate station names for display
    function shortName(n) {
        if (!n) return '?';
        n = n.replace(/ Station$/i, '');
        if (n.length > 10) n = n.substring(0, 10);
        return n;
    }

    // Format countdown body text
    function formatCountdown() {
        if (!dep || !dep.departure_time) return null;

        // Parse UTC time - server sends ISO format like "2025-12-14T07:59:00+00:00"
        // Some JS engines don't parse +00:00 correctly, so normalize to Z
        var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
        var depTime = new Date(timeStr);
        var now = new Date();
        var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);

        // Debug: log if something looks wrong
        if (diffSec < -60 || diffSec > 86400) {
            console.log('Time calc may be wrong: departure=' + dep.departure_time +
                ', parsed=' + depTime.toISOString() + ', now=' + now.toISOString() +
                ', diff=' + diffSec + 's');
        }

        var bodyText = '';
        if (diffSec <= 0) {
            bodyText = 'Arriving NOW!';
        } else {
            var mins = Math.floor(diffSec / 60);
            var secs = diffSec % 60;
            if (mins > 0) {
                bodyText = mins + ' min ' + secs + ' sec';
            } else {
                bodyText = secs + ' seconds';
            }
        }

        if (dep.platform) {
            bodyText += '\nPlatform ' + dep.platform;
        }
        return bodyText;
    }

    // Build title: "Narre > City"
    var title = shortName(name) + ' > ' + shortName(destName);

    loadingCard.title(title);

    if (dep && dep.message) {
        loadingCard.subtitle('');

        // Initial update
        var bodyText = formatCountdown();
        if (!bodyText) {
            // Fallback if no departure_time
            if (dep.minutes !== null && dep.minutes !== undefined) {
                bodyText = dep.minutes === 0 ? 'Arriving NOW!' : dep.minutes + ' minute' + (dep.minutes !== 1 ? 's' : '');
            } else {
                bodyText = dep.message;
            }
            if (dep.platform) {
                bodyText += '\nPlatform ' + dep.platform;
            }
        }
        loadingCard.body(bodyText);

        // Start live countdown timer (update every second)
        if (dep.departure_time) {
            countdownTimer = setInterval(function () {
                var newText = formatCountdown();
                if (newText) {
                    loadingCard.body(newText);
                }
            }, 1000);
        }

        // Get vibration pattern from server
        if (dep.minutes !== null && dep.minutes !== undefined) {
            sendStealthQuery(stopId, Settings.option('btn' + buttonIndex + '_route_type'),
                Settings.option('btn' + buttonIndex + '_direction_id'),
                function (response) {
                    if (response.vibration) {
                        playVibrationPattern(response.vibration);
                    }
                },
                function () {
                    Vibe.vibrate('short');
                });
        } else {
            Vibe.vibrate('short');
        }
    } else {
        loadingCard.subtitle('');
        loadingCard.body('No data yet\nWaiting for update...');
        Vibe.vibrate('short');
    }

    loadingCard.show();

    // Auto-hide after 5 seconds (longer to enjoy the countdown)
    setTimeout(function () {
        if (countdownTimer) {
            clearInterval(countdownTimer);
            countdownTimer = null;
        }
        loadingCard.hide();
    }, 5000);
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
    var serverUrl = DEFAULT_SERVER_URL;
    var apiKey = Settings.option('api_key') || '';

    console.log('Using server URL: ' + serverUrl);

    // Check if API key is configured
    if (!apiKey) {
        loadingCard.title('Setup Required');
        loadingCard.subtitle('');
        loadingCard.body('Open settings in\nthe Pebble app\nand enter your\nAPI key');
        loadingCard.show();
        return;
    }

    // Convert to WebSocket URL with API key
    var wsUrl = serverUrl
        .replace('https://', 'wss://')
        .replace('http://', 'ws://')
        .replace(/\/+$/, '') + '/ws';

    // Add API key as query parameter
    wsUrl += '?api_key=' + encodeURIComponent(apiKey);

    // Build buttons query param for instant data on connect
    // Format: "1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID"
    var buttonParts = [];
    for (var i = 1; i <= 3; i++) {
        var stopId = Settings.option('btn' + i + '_stop_id');
        if (stopId) {
            var routeType = Settings.option('btn' + i + '_route_type') || 0;
            var directionId = Settings.option('btn' + i + '_direction_id');
            var part = i + ':' + stopId + ':' + routeType;
            if (directionId !== undefined && directionId !== null) {
                part += ':' + directionId;
            }
            buttonParts.push(part);
        }
    }
    if (buttonParts.length > 0) {
        wsUrl += '&buttons=' + encodeURIComponent(buttonParts.join(','));
    }

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
    var hasReceivedData = false;
    var menuShowTimer = null;

    ws.onopen = function () {
        wsConnected = true;
        console.log('WebSocket connected');

        // If no buttons configured, show menu immediately (no data to wait for)
        if (buttonParts.length === 0) {
            console.log('No buttons configured, showing menu immediately');
            mainMenu.items(0, buildMenuItems());
            loadingCard.hide();
            mainMenu.show();
            isFirstLoad = false;
            return;
        }

        loadingCard.subtitle('Loading...');

        // Set fallback timer - show menu after 2s even if no stealth_update received
        menuShowTimer = setTimeout(function () {
            if (isFirstLoad) {
                console.log('Fallback: showing menu without stealth data');
                mainMenu.items(0, buildMenuItems());
                loadingCard.hide();
                mainMenu.show();
                isFirstLoad = false;
            }
        }, 2000);
    };

    ws.onclose = function (e) {
        wsConnected = false;
        console.log('WebSocket closed, code: ' + e.code);

        if (menuShowTimer) {
            clearTimeout(menuShowTimer);
            menuShowTimer = null;
        }

        // Code 4001 = invalid API key (custom code from server)
        if (e.code === 4001) {
            loadingCard.title('Invalid API Key');
            loadingCard.subtitle('');
            loadingCard.body('Please check your\nAPI key in settings');
            loadingCard.show();
            return; // Don't auto-reconnect for auth errors
        }

        // Auto-reconnect for other disconnects
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = function (e) {
        console.log('WebSocket error');
        loadingCard.title('Connection Failed');
        loadingCard.subtitle('');
        loadingCard.body('Check your\ninternet connection');
    };

    ws.onmessage = function (event) {
        try {
            var msg = JSON.parse(event.data);

            // Handle live stealth updates (broadcast, no pending request)
            if (msg.type === 'stealth_update') {
                var updates = msg.updates || [];
                for (var i = 0; i < updates.length; i++) {
                    var u = updates[i];
                    buttonDepartures[u.button_id] = {
                        minutes: u.minutes,
                        platform: u.platform,
                        message: u.message,
                        departure_time: u.departure_time
                    };
                }
                // Refresh menu to show updated times
                mainMenu.items(0, buildMenuItems());

                // On first stealth_update, show menu immediately (data is ready!)
                if (isFirstLoad && !hasReceivedData) {
                    hasReceivedData = true;
                    if (menuShowTimer) {
                        clearTimeout(menuShowTimer);
                        menuShowTimer = null;
                    }
                    console.log('Received stealth data, showing menu');
                    loadingCard.hide();
                    mainMenu.show();
                    isFirstLoad = false;
                }
                return;
            }

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

    // Also save dest_name if provided
    if (config.dest_name) {
        Settings.option('btn' + btnId + '_dest_name', config.dest_name);
    }

    if (config.direction_id !== undefined && config.direction_id !== null) {
        Settings.option('btn' + btnId + '_direction_id', config.direction_id);
    }

    // Clear stale departure cache for this button so we don't use old data
    delete buttonDepartures[btnId];

    // Reconnect WebSocket with new buttons in URL to get fresh data
    // This ensures only ONE subscription mechanism (URL params) is used
    reconnect();

    // Refresh menu to show new button configuration
    mainMenu.items(0, buildMenuItems());

    // Vibrate to confirm
    Vibe.vibrate('short');
    console.log('Button ' + btnId + ' saved successfully');
}

// Subscribe to live stealth updates
function sendStealthSubscription() {
    if (!ws || !wsConnected) return;

    var buttons = [];
    for (var i = 1; i <= 3; i++) {
        var stopId = Settings.option('btn' + i + '_stop_id');
        if (stopId) {
            buttons.push({
                button_id: i,
                stop_id: stopId,
                route_type: Settings.option('btn' + i + '_route_type') || 0,
                direction_id: Settings.option('btn' + i + '_direction_id')
            });
        }
    }

    if (buttons.length > 0) {
        console.log('Subscribing to stealth updates for ' + buttons.length + ' buttons');
        ws.send(JSON.stringify({
            type: 'subscribe_stealth',
            buttons: buttons
        }));
    }
}

// Start the app
console.log('PTV Notify starting...');
connectWebSocket();