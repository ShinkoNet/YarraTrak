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
var Window = require('ui/window');
var Text = require('ui/text');
var Rect = require('ui/rect');
var Vector2 = require('vector2');

// Custom vibration pattern player
// Pebble.js only supports 'short', 'long', 'double' - we simulate patterns
// Pattern format: [vibe_ms, pause_ms, vibe_ms, pause_ms, ...]
var vibeTimer = null;

function playVibrationPattern(pattern) {
    // Clear any ongoing pattern
    if (vibeTimer) {
        // console.log('Clearing existing vibeTimer');
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

    console.log('Starting vibration pattern: ' + JSON.stringify(pattern));
    playNext();
}

// Calculate vibration pattern locally (no network call needed)
// Encodes minutes as haptic pattern: Hours=1000ms, Tens=500ms, Ones=150ms
function calculateVibration(minutes) {
    if (minutes === 0) {
        return [
            500, 150,
            150, 150,
            150, 150,
            500, 150,
            500, 500,
            500, 150,
            500
        ];
    }

    minutes = Math.max(0, Math.min(720, minutes));
    var hours = Math.floor(minutes / 60);
    var tens = Math.floor((minutes % 60) / 10);
    var ones = minutes % 10;

    var pattern = [];
    for (var i = 0; i < hours; i++) pattern.push(1000, 400);
    if (hours > 0 && (tens > 0 || ones > 0) && pattern.length) pattern[pattern.length - 1] += 200;

    for (var i = 0; i < tens; i++) pattern.push(500, 300);
    if (tens > 0 && ones > 0 && pattern.length) pattern[pattern.length - 1] += 100;

    for (var i = 0; i < ones; i++) pattern.push(150, 150);

    console.log('calculateVibration: ' + minutes + ' mins -> ' + JSON.stringify(pattern));
    return pattern;
}

var Vector2 = require('vector2');

// Server configuration - update this URL to your server
var CONFIG_URL = 'https://ptv.netcavy.net/pebble-config.html';

// Application state

var ws = null;
var wsConnected = false;
var reconnectTimer = null;
var sessionId = generateUUID();
var queryHistory = [];
var messageId = 0;
var pendingRequests = {};

// Cancel all pending queries (on disconnect or new query)
function cancelPendingQueries() {
    for (var id in pendingRequests) {
        if (pendingRequests[id].timeout) {
            clearTimeout(pendingRequests[id].timeout);
        }
    }
    pendingRequests = {};
}

// Calculate total duration of a vibration pattern
function getPatternDuration(pattern) {
    if (!Array.isArray(pattern)) return 0;
    var total = 0;
    for (var i = 0; i < pattern.length; i++) total += pattern[i];
    console.log('getPatternDuration: ' + total + 'ms for ' + JSON.stringify(pattern));
    return total;
}

// Live departure data from server broadcasts: {1: {departures: [{minutes, platform, departure_time}, ...]}, ...}
var buttonDepartures = {};

// Station watching mode - keep panel open and vibrate on minute changes
var watchingButtonIndex = null;  // Which button is being watched (null = not watching)
var lastVibratedMinutes = null;  // Last minute count we vibrated (to detect changes)
var lastDepartureTime = null;    // Track departure_time to detect train transitions
var watchingRouteText = null;    // Current route text for display (stored globally for stealth_update)

// Get the current valid departure from the cached array
// Auto-switches to next departure when first train has passed
function getCurrentDeparture(buttonIndex) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.departures || cache.departures.length === 0) {
        return null;
    }

    var now = new Date();
    for (var i = 0; i < cache.departures.length; i++) {
        var dep = cache.departures[i];
        if (dep.departure_time) {
            // Parse UTC time
            var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
            var depTime = new Date(timeStr);
            // If departure is in the future (or "arriving now" with <60s passed), use it
            if (depTime.getTime() > now.getTime() - 60000) {
                return dep;
            }
        } else if (dep.minutes !== null && dep.minutes !== undefined) {
            // Fallback if no departure_time - just return first with minutes
            return dep;
        }
    }
    // All departures have passed
    return null;
}

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
        console.log('Config closed');

        // Clear stale departure cache - button configs may have changed
        buttonDepartures = {};

        // Always refresh menu items to show new button config immediately
        mainMenu.items(0, buildMenuItems());

        // Reconnect WebSocket with new buttons in URL to get fresh data
        reconnect();
    }
);

// Loading screen (used during WebSocket connection)
var loadingCard = new UI.Card({
    title: 'PTV Notify',
    subtitle: 'Starting...',
    scrollable: false
});

// Custom watching window with big timer
// Screen layout (144x168 Aplite): timer big at top, platform medium, route small at bottom
var watchingWindow = new Window({
    backgroundColor: 'black'
});

// Get screen dimensions
var screenWidth = Feature.resolution().x;  // 144 for Aplite
var screenHeight = Feature.resolution().y; // 168 for Aplite

// Timer text - BIG monospace (top of screen)
var timerText = new Text({
    position: new Vector2(0, 15),
    size: new Vector2(screenWidth, 55),
    font: 'leco-42-numbers',
    color: 'white',
    textAlign: 'center',
    text: '...'
});
watchingWindow.add(timerText);

// Platform text - medium (middle)
var platformText = new Text({
    position: new Vector2(0, 75),
    size: new Vector2(screenWidth, 30),
    font: 'gothic-24-bold',
    color: 'white',
    textAlign: 'center',
    text: ''
});
watchingWindow.add(platformText);

// Route text - small (bottom, 2 lines max)
var routeText = new Text({
    position: new Vector2(5, 115),
    size: new Vector2(screenWidth - 10, 50),
    font: 'gothic-18',
    color: 'lightGray',
    textAlign: 'center',
    text: ''
});
watchingWindow.add(routeText);

// Progress bar - shows seconds visually (shrinks as time passes)
var progressBar = new Rect({
    position: new Vector2(0, screenHeight - 6),
    size: new Vector2(screenWidth, 4),
    backgroundColor: 'white'
});
watchingWindow.add(progressBar);

// Back button handler for watching window
watchingWindow.on('click', 'back', function () {
    stopWatching();
});
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
            var dep = getCurrentDeparture(i);
            var subtitle = 'Waiting...';
            if (dep) {
                if (dep.minutes === 0) {
                    subtitle = 'Now';
                } else if (dep.minutes !== null && dep.minutes !== undefined) {
                    if (dep.minutes >= 60) {
                        var hrs = Math.floor(dep.minutes / 60);
                        var mins = dep.minutes % 60;
                        subtitle = hrs + 'hr ' + mins + 'm';
                    } else {
                        subtitle = dep.minutes + ' min';
                    }
                }
                if (dep.platform) {
                    subtitle += ' • P' + dep.platform;
                }
            }
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

// Reusable voice query card (created once, reused to prevent timer leaks)
var voiceCard = new UI.Card({
    title: 'Voice Query',
    scrollable: true
});

voiceCard.on('click', 'select', function () {
    startVoiceQuery();
});

voiceCard.on('click', 'back', function () {
    voiceCard.hide();
});

// Voice query flow
// Voice query flow
function startVoiceQuery() {
    // Cancel any pending queries to prevent memory buildup
    cancelPendingQueries();

    // Don't show card yet - let dictation happen on top of current view
    // This avoids window stack conflicts and memory pressure

    Voice.dictate('start', true, function (e) {
        if (e.err) {
            if (e.err === 'systemAborted') {
                return;
            }
            voiceCard.title('Error');
            voiceCard.body('Dictation: ' + e.err);
            voiceCard.show(); // Show error if it wasn't shown
            return;
        }

        voiceCard.title('Processing...');
        voiceCard.subtitle('');
        voiceCard.body('You: ' + e.transcription);
        voiceCard.show(); // Show card only now, after dictation implies success

        // Small delay before WebSocket call - helps Pebble.js stability
        setTimeout(function () {
            sendQuery(e.transcription, function (response) {
                handleQueryResponse(voiceCard, response);
            }, function (error) {
                voiceCard.title('Error');
                voiceCard.body(error.message || 'Query failed');
            });
        }, 100);
    });
}

// Countdown timer for live seconds display
var countdownTimer = null;

// Stealth query - uses cached live departure data, opens station watching mode
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

    // Enter station watching mode
    watchingButtonIndex = buttonIndex;

    // Use cached live departure data - auto-switches between departures
    var dep = getCurrentDeparture(buttonIndex);

    // Clean station names (remove " Station" suffix but keep full name)
    function cleanName(n) {
        if (!n) return '?';
        return n.replace(/ Station$/i, '');
    }

    // Build route text for body (stored globally for stealth_update handler)
    watchingRouteText = cleanName(name) + ' > ' + cleanName(destName);

    if (dep && (dep.departure_time || dep.minutes !== null)) {
        // Update display with route info
        updateWatchingDisplay(dep, watchingRouteText);

        // Calculate fresh minutes from departure_time
        var freshMinutes = null;
        if (dep.departure_time) {
            var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
            var depTime = new Date(timeStr);
            var now = new Date();
            var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);
            freshMinutes = Math.max(0, Math.floor(diffSec / 60));
        } else if (dep.minutes !== null && dep.minutes !== undefined) {
            freshMinutes = dep.minutes;
        }

        // Update display
        updateWatchingDisplay(dep, watchingRouteText);

        // Start live countdown timer (update every second, vibrate on minute change)
        if (dep.departure_time) {
            countdownTimer = setInterval(function () {
                var currentDep = getCurrentDeparture(watchingButtonIndex);
                if (currentDep) {
                    updateWatchingDisplay(currentDep, watchingRouteText);

                    // Check for minute change - vibrate instantly when crossing minute boundary
                    if (currentDep.departure_time) {
                        var timeStr = currentDep.departure_time.replace(/\+00:00$/, 'Z');
                        var depTime = new Date(timeStr);
                        var now = new Date();
                        var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);
                        var currentMinutes = Math.max(0, Math.floor(diffSec / 60));

                        // Check if this is a new train (departure_time changed)
                        var isNewTrain = lastDepartureTime && currentDep.departure_time !== lastDepartureTime;

                        if (isNewTrain) {
                            // Train transition - vibrate the new train's time
                            console.log('[Watch] New train detected: ' + currentMinutes + ' mins');
                            lastVibratedMinutes = currentMinutes;
                            lastDepartureTime = currentDep.departure_time;
                            playVibrationPattern(calculateVibration(currentMinutes));
                        } else if (lastVibratedMinutes !== null && currentMinutes < lastVibratedMinutes) {
                            // Minute decreased - vibrate!
                            console.log('[Watch] Minute boundary: ' + lastVibratedMinutes + ' -> ' + currentMinutes);
                            lastVibratedMinutes = currentMinutes;
                            playVibrationPattern(calculateVibration(currentMinutes));
                        }
                    }
                }
            }, 1000);
        }

        // Initial vibration
        if (freshMinutes !== null) {
            console.log('[Watch] Starting watch, initial minutes: ' + freshMinutes);
            lastVibratedMinutes = freshMinutes;
            lastDepartureTime = dep.departure_time;
            playVibrationPattern(calculateVibration(freshMinutes));
        } else {
            lastVibratedMinutes = null;
            lastDepartureTime = null;
            Vibe.vibrate('short');
        }
    } else {
        timerText.text('...');
        platformText.text('');
        routeText.text(watchingRouteText);
        lastVibratedMinutes = null;
        lastDepartureTime = null;
        Vibe.vibrate('short');
    }

    watchingWindow.show();
    // Panel stays open until user presses back - no auto-hide!
}

// Update the watching display with current departure info
// Uses custom watchingWindow with separate Text elements
function updateWatchingDisplay(dep, route) {
    if (!dep) {
        timerText.text('...');
        platformText.text('');
        routeText.text(route || '');
        progressBar.size(new Vector2(screenWidth, 4));  // Full bar when waiting
        return;
    }

    var timerValue = '';
    var timerFont = 'leco-42-numbers';  // Default: monospace numbers
    var progressWidth = screenWidth;  // Default full width

    if (dep.departure_time) {
        var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
        var depTime = new Date(timeStr);
        var now = new Date();
        var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);

        if (diffSec <= 0) {
            timerValue = 'NOW!';
            timerFont = 'bitham-42-bold';  // Switch to font with letters
            progressWidth = 0;  // Empty bar when arriving
        } else {
            var totalMins = Math.floor(diffSec / 60);
            var secs = diffSec % 60;
            if (totalMins >= 60) {
                // Show H:MM:SS for 60+ minutes
                var hrs = Math.floor(totalMins / 60);
                var mins = totalMins % 60;
                timerValue = hrs + ':' + (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
                // Progress bar: shrinks with seconds
                progressWidth = Math.floor(screenWidth * secs / 60);
            } else if (totalMins > 0) {
                timerValue = totalMins + ':' + (secs < 10 ? '0' : '') + secs;
                // Progress bar: shrinks with seconds
                progressWidth = Math.floor(screenWidth * secs / 60);
            } else {
                // Under 1 minute: show NOW! (matches menu subtitle)
                timerValue = 'NOW!';
                timerFont = 'bitham-42-bold';
                progressWidth = 0;  // Hide progress bar for NOW!
            }
        }
    } else if (dep.minutes !== null && dep.minutes !== undefined) {
        if (dep.minutes === 0) {
            timerValue = 'NOW!';
            timerFont = 'bitham-42-bold';
        } else {
            if (dep.minutes >= 60) {
                var hrs = Math.floor(dep.minutes / 60);
                var mins = dep.minutes % 60;
                timerValue = hrs + ':' + (mins < 10 ? '0' : '') + mins;
            } else {
                timerValue = dep.minutes + ' min';
            }
        }
        progressWidth = dep.minutes === 0 ? 0 : screenWidth;
    } else {
        timerValue = '...';
    }

    timerText.font(timerFont);
    timerText.text(timerValue);
    platformText.text(dep.platform ? 'Platform ' + dep.platform : '');
    routeText.text(route || '');
    progressBar.size(new Vector2(progressWidth, 4));
}

// Stop station watching mode
function stopWatching() {
    console.log('[Watch] Stopping watch mode');
    watchingButtonIndex = null;
    lastVibratedMinutes = null;
    lastDepartureTime = null;
    if (countdownTimer) {
        clearInterval(countdownTimer);
        countdownTimer = null;
    }
    if (vibeTimer) {
        clearTimeout(vibeTimer);
        vibeTimer = null;
    }
    watchingWindow.hide();
}

// Handle query response
function handleQueryResponse(card, response) {
    // Server sends {type, payload} directly, or we get {data: {type, payload}}
    // Handle both formats for backwards compatibility
    var data = response.data || response;

    if (!data || !data.type) {
        card.title('Error');
        card.body('No response');
        return;
    }

    if (data.type === 'RESULT') {
        var payload = data.payload;
        card.title('Departures');
        card.body(payload.tts_text || 'No info');

        // Calculate vibration locally from departure time
        var departure = payload.departure;
        if (departure && departure.minutes_to_depart !== undefined) {
            playVibrationPattern(calculateVibration(departure.minutes_to_depart));
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

        // Clear stale pending requests - they won't complete anyway
        cancelPendingQueries();

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
                    // Store full departures array for smart switching
                    buttonDepartures[u.button_id] = {
                        departures: u.departures || []  // Array of {minutes, platform, departure_time}
                    };

                    // Station watching mode: vibrate when minutes change
                    if (watchingButtonIndex === u.button_id) {
                        var dep = getCurrentDeparture(u.button_id);
                        if (dep) {
                            // Calculate fresh minutes
                            var freshMinutes = null;
                            if (dep.departure_time) {
                                var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
                                var depTime = new Date(timeStr);
                                var now = new Date();
                                var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);
                                freshMinutes = Math.max(0, Math.floor(diffSec / 60));
                            } else if (dep.minutes !== null) {
                                freshMinutes = dep.minutes;
                            }

                            if (freshMinutes !== null) {
                                // Check if this is a new train (departure_time changed)
                                var isNewTrain = lastDepartureTime && dep.departure_time !== lastDepartureTime;
                                // Check if minutes decreased OR we switched to a new train
                                var shouldVibrate = (lastVibratedMinutes !== null && freshMinutes < lastVibratedMinutes) || isNewTrain;

                                if (shouldVibrate) {
                                    console.log('[Watch] Minute change: ' + lastVibratedMinutes + ' -> ' + freshMinutes + (isNewTrain ? ' (new train)' : ''));
                                    lastVibratedMinutes = freshMinutes;
                                    lastDepartureTime = dep.departure_time;
                                    playVibrationPattern(calculateVibration(freshMinutes));
                                } else if (lastVibratedMinutes === null) {
                                    // First data after "waiting" state
                                    console.log('[Watch] First data received: ' + freshMinutes + ' mins');
                                    lastVibratedMinutes = freshMinutes;
                                    lastDepartureTime = dep.departure_time;
                                    playVibrationPattern(calculateVibration(freshMinutes));
                                }
                            }
                            // Update display
                            updateWatchingDisplay(dep, watchingRouteText);
                        }
                    }
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
                    // Don't hide loadingCard if we're in watching mode
                    if (watchingButtonIndex === null) {
                        loadingCard.hide();
                    }
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
    // Clear stale departure cache to avoid showing old data after config changes
    buttonDepartures = {};
    connectWebSocket();
}

// Send query
function sendQuery(text, successCb, errorCb) {
    console.log('sendQuery: start');
    if (!ws || !wsConnected) {
        console.log('sendQuery: not connected');
        errorCb({ message: 'Not connected' });
        return;
    }

    var id = String(++messageId);
    console.log('sendQuery: id=' + id);

    pendingRequests[id] = {
        successCb: successCb,
        errorCb: errorCb,
        timeout: setTimeout(function () {
            delete pendingRequests[id];
            errorCb({ message: 'Timeout' });
        }, 30000)
    };

    console.log('sendQuery: sending...');

    // Minimal payload to test if large queryHistory is causing crash
    ws.send(JSON.stringify({
        type: 'query',
        id: id,
        text: text,
        session_id: sessionId
        // queryHistory temporarily removed to test memory issue
    }));

    console.log('sendQuery: sent');
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

    // Refresh menu to show new button configuration
    mainMenu.items(0, buildMenuItems());

    // Vibrate to confirm
    Vibe.vibrate('short');
    console.log('Button ' + btnId + ' saved successfully');

    // Reconnect WebSocket with new buttons in URL to get fresh data
    // Delayed to allow LLM response panel to display first
    setTimeout(function () {
        reconnect();
    }, 500);
}

// Start the app
console.log('PTV Notify starting...');
connectWebSocket();