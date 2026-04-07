/**
 * PTV Notify - Melbourne Public Transport for Pebble
 * 
 * Voice-enabled departure queries via WebSocket.
 */

var UI = require('ui');
var Settings = require('settings');
var Vibe = require('ui/vibe');
var Feature = require('platform/feature');
var ajax = require('ajax');
var Window = require('ui/window');
var Text = require('ui/text');
var Rect = require('ui/rect');
var Vector2 = require('vector2');
var Image = require('ui/image');

// Custom vibration pattern player
// Pebble.js only supports 'short', 'long', 'double' - we simulate patterns
// Pattern format: [vibe_ms, pause_ms, vibe_ms, pause_ms, ...]
var vibeTimer = null;

function playVibrationPattern(pattern) {
    // Clear any ongoing pattern (legacy timer)
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

    console.log('Sending custom vibration pattern: ' + JSON.stringify(pattern));
    Vibe.vibrateCustom(pattern);
}

// Calculate vibration pattern locally (no network call needed)
// Encodes minutes as haptic pattern: Hours=1000ms, Tens=500ms, Ones=150ms
function calculateVibration(minutes) {
    if (minutes === 0) {
        return [43, 300, 43, 71, 43, 43, 43, 100, 43, 300, 43, 643, 43, 300, 43];
    }

    minutes = Math.max(0, Math.min(720, minutes));
    var hours = Math.floor(minutes / 60);
    var tens = Math.floor((minutes % 60) / 10);
    var ones = minutes % 10;

    var pattern = [];
    for (var i = 0; i < hours; i++) pattern.push(800, 300);
    if (hours > 0 && (tens > 0 || ones > 0) && pattern.length) pattern[pattern.length - 1] += 200;

    for (var i = 0; i < tens; i++) pattern.push(300, 150);
    if (tens > 0 && ones > 0 && pattern.length) pattern[pattern.length - 1] += 100;

    for (var i = 0; i < ones; i++) pattern.push(80, 180);

    console.log('calculateVibration: ' + minutes + ' mins -> ' + JSON.stringify(pattern));
    return pattern;
}

var Vector2 = require('vector2');

// Server configuration - update this URL to your server
var CONFIG_URL = 'https://ptv.netcavy.net/pebble-config.html';

// Application state

var ws = null;
var wsConnected = false;
var wsConnecting = false;
var reconnectTimer = null;
var wsConnectGeneration = 0;
var heartbeatTimer = null;
var lastPongAt = 0;
var sessionId = generateUUID();
var queryHistory = [];
var messageId = 0;
var pendingRequests = {};
var voiceModule = null;
var hasShownMainMenu = false;
var loadingAnimationTimer = null;
var loadingDiagnosticTimer = null;
var loadingDiagnosticToken = 0;
var loadingDiagnosticsInFlight = false;
var loadingBaseText = 'Connecting to PTV';
var loadingDotCount = 0;

// Cancel all pending queries (on disconnect or new query)
function cancelPendingQueries() {
    for (var id in pendingRequests) {
        if (pendingRequests[id].timeout) {
            clearTimeout(pendingRequests[id].timeout);
        }
    }
    pendingRequests = {};
}

function getVoice() {
    if (!voiceModule) {
        voiceModule = require('ui/voice');
    }
    return voiceModule;
}

function getConfiguredEntryCount() {
    var entryCount = Settings.option('entry_count');
    if (entryCount === undefined || entryCount === null) {
        entryCount = 0;
        for (var i = 1; i <= 10; i++) {
            if (Settings.option('entry' + i + '_stop_id') || Settings.option('btn' + i + '_stop_id')) {
                entryCount = i;
            }
        }
    }
    return entryCount;
}

function clearLoadingTimers() {
    if (loadingAnimationTimer) {
        clearInterval(loadingAnimationTimer);
        loadingAnimationTimer = null;
    }
    if (loadingDiagnosticTimer) {
        clearTimeout(loadingDiagnosticTimer);
        loadingDiagnosticTimer = null;
    }
    loadingDiagnosticsInFlight = false;
}

function stopHeartbeat() {
    if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
    }
    lastPongAt = 0;
}

function startHeartbeat(socket, connectGeneration) {
    stopHeartbeat();
    lastPongAt = Date.now();
    heartbeatTimer = setInterval(function () {
        if (ws !== socket || connectGeneration !== wsConnectGeneration || !wsConnected) {
            stopHeartbeat();
            return;
        }

        var now = Date.now();
        if (lastPongAt && (now - lastPongAt) > 70000) {
            console.log('WebSocket heartbeat timed out');
            reconnect();
            return;
        }

        try {
            socket.send(JSON.stringify({
                type: 'ping',
                id: null
            }));
        } catch (err) {
            console.log('WebSocket heartbeat send failed: ' + err);
            reconnect();
        }
    }, 25000);
}

function getServerHealthUrl(serverUrl) {
    return serverUrl.replace(/\/+$/, '') + '/api/v1/health';
}

function isConnectionBannerVisible() {
    return hasShownMainMenu && (!wsConnected || wsConnecting);
}

function getConnectionBannerItem() {
    if (!isConnectionBannerVisible()) {
        return null;
    }

    return {
        title: wsConnecting ? 'Reconnecting...' : 'Offline',
        subtitle: wsConnecting ? 'Refreshing live data' : 'Showing last known times',
        data: { system: true }
    };
}

function refreshConnectionUi() {
    if (hasShownMainMenu) {
        mainMenu.items(0, buildMenuItems());
    }

    if (watchingButtonIndex !== null) {
        updateWatchingDisplay(getWatchedDeparture(watchingButtonIndex), watchingRouteText);
    }
}

function setLoadingHeadline() {
    var dots = '';
    for (var i = 0; i < loadingDotCount; i++) {
        dots += '.';
    }
    splashStatusText.text(loadingBaseText + dots);
}

function setLoadingDetail(text) {
    var hasDetail = !!text;
    loadingDetailBacking.backgroundColor(hasDetail ? 'black' : 'clear');
    loadingDetailBacking.borderColor(hasDetail ? 'white' : 'clear');
    loadingDetailText.text(text || '');
}

function runLoadingDiagnostics(serverUrl, token) {
    if (token !== loadingDiagnosticToken || hasShownMainMenu || loadingDiagnosticsInFlight) {
        return;
    }

    loadingDiagnosticsInFlight = true;
    setLoadingDetail('Checking connectivity...');

    ajax({
        url: 'https://connectivitycheck.gstatic.com/generate_204',
        method: 'GET',
        cache: false
    }, function () {
        if (token !== loadingDiagnosticToken || hasShownMainMenu) {
            loadingDiagnosticsInFlight = false;
            return;
        }

        ajax({
            url: getServerHealthUrl(serverUrl),
            method: 'GET',
            cache: false
        }, function () {
            loadingDiagnosticsInFlight = false;
            if (token !== loadingDiagnosticToken || hasShownMainMenu) {
                return;
            }
            setLoadingDetail('');
        }, function () {
            loadingDiagnosticsInFlight = false;
            if (token !== loadingDiagnosticToken || hasShownMainMenu) {
                return;
            }
            setLoadingDetail('PTV endpoint is currently down.\nSorry!!');
        });
    }, function () {
        loadingDiagnosticsInFlight = false;
        if (token !== loadingDiagnosticToken || hasShownMainMenu) {
            return;
        }
        setLoadingDetail('Please connect to the internet.');
    });
}

// Calculate total duration of a vibration pattern
function getPatternDuration(pattern) {
    if (!Array.isArray(pattern)) return 0;
    var total = 0;
    for (var i = 0; i < pattern.length; i++) total += pattern[i];
    console.log('getPatternDuration: ' + total + 'ms for ' + JSON.stringify(pattern));
    return total;
}

// Live departure data from server broadcasts:
// {1: {departures: [...], disruptionLabel: 'Bus Replacements', disruptionLabels: ['Bus Replacements', 'Major Delays 25m']}, ...}
var buttonDepartures = {};

// Station watching mode - keep panel open and vibrate on minute changes
var watchingButtonIndex = null;  // Which button is being watched (null = not watching)
var lastVibratedMinutes = null;  // Last minute count we vibrated (to detect changes)
var lastDepartureTime = null;    // Track departure_time to detect train transitions
var watchingRouteText = null;    // Current route text for display (stored globally for favourite_update)
var watchingDistanceKm = null;   // Latest distance to stop (km)
var watchingVehicleDesc = null;  // Latest vehicle descriptor
var watchingRunRef = null;       // Current watched run_ref
var watchingSelectedRunRef = null; // Selected departure identity for service-after tracking
var watchingSelectedDepartureTime = null; // Selected departure time for service-after tracking
var watchingStopId = null;       // Current watched stop_id
var watchingDistanceRunRef = null; // Run_ref associated with distance
var lastRenderedWatchTimerValue = null; // Last timer string shown on the watch page
var lastRenderedWatchTimerMetric = null; // Last comparable countdown value for watch page
var watchTimerAnimationTimers = []; // Active timer text animation timeouts

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

function getCurrentDisruptionLabel(buttonIndex) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.disruptionLabel) {
        return null;
    }
    return cache.disruptionLabel;
}

function getCurrentDisruptionLabels(buttonIndex) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.disruptionLabels || cache.disruptionLabels.length === 0) {
        var fallback = getCurrentDisruptionLabel(buttonIndex);
        return fallback ? [fallback] : [];
    }
    return cache.disruptionLabels;
}

function getMenuDisruptionLabel(label) {
    if (!label) {
        return null;
    }
    if (label.indexOf('Bus Replacements') === 0) {
        return 'Bus Replcmnt';
    }
    if (label.indexOf('Major Delays') === 0) {
        return 'Major Delays';
    }
    if (label.indexOf('Minor Delays') === 0) {
        return 'Minor Delays';
    }
    return label;
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

function getOrCreateClientId() {
    var clientId = Settings.option('client_id');
    if (!clientId) {
        clientId = generateUUID();
        Settings.option('client_id', clientId);
        console.log('Generated client_id: ' + clientId);
    }
    return clientId;
}

// Initialize settings with defaults
var DEFAULT_SERVER_URL = 'https://ptv.netcavy.net';

// Demo entries for first launch (Rebble App Contest)
var DEMO_ENTRIES = [
    {
        name: 'Flinders St',
        full_name: 'Flinders Street Station',
        stop_id: 1071,
        dest_name: 'Belgrave',
        full_dest_name: 'Belgrave Station',
        dest_id: 1018,
        route_type: 0,
        direction_id: 2,
        direction_name: 'Belgrave'
    },
    {
        name: 'Caulfield',
        full_name: 'Caulfield Station',
        stop_id: 1036,
        dest_name: 'Town Hall',
        full_dest_name: 'Town Hall Station',
        dest_id: 1235,
        route_type: 0,
        direction_id: 1,
        direction_name: 'City'
    },
    {
        name: 'Barkly Sq/Syd Rd',
        full_name: 'Barkly Square/Sydney Rd #20',
        stop_id: 2811,
        dest_name: 'QVM/Eliz St',
        full_dest_name: 'Queen Victoria Market/Elizabeth St #7',
        dest_id: 2258,
        route_type: 1,
        direction_id: 11,
        direction_name: 'Flinders St'
    },
    {
        name: 'Sth Cross',
        full_name: 'Southern Cross Railway Station',
        stop_id: 1181,
        dest_name: 'Bendigo',
        full_dest_name: 'Bendigo Railway Station',
        dest_id: 1509,
        route_type: 3,
        direction_id: 6,
        direction_name: 'Bendigo'
    }
];

function populateDemoEntries() {
    console.log('First launch: populating demo entries');
    Settings.option('entry_count', DEMO_ENTRIES.length);
    for (var i = 0; i < DEMO_ENTRIES.length; i++) {
        var d = DEMO_ENTRIES[i];
        var n = i + 1;
        Settings.option('entry' + n + '_name', d.name);
        Settings.option('entry' + n + '_full_name', d.full_name || d.name);
        Settings.option('entry' + n + '_stop_id', d.stop_id);
        Settings.option('entry' + n + '_dest_name', d.dest_name);
        Settings.option('entry' + n + '_full_dest_name', d.full_dest_name || d.dest_name);
        Settings.option('entry' + n + '_dest_id', d.dest_id);
        Settings.option('entry' + n + '_route_type', d.route_type);
        Settings.option('entry' + n + '_direction_id', d.direction_id);
    }
}

// Auto-populate demo entries on first launch
if (!Settings.option('entry_count') && !Settings.option('entry1_stop_id') && !Settings.option('btn1_stop_id')) {
    populateDemoEntries();
}

getOrCreateClientId();

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

var screenWidth = Feature.resolution().x;  // 144 for Aplite
var screenHeight = Feature.resolution().y; // 168 for Aplite
var useTextOnlySplash = Feature.platform({ aplite: true, flint: true }, true, false);
var appBackgroundColor = Feature.color('#291381', 'black');
var secondaryTextColor = Feature.color('lightGray', 'white');
var warningTextColor = Feature.color('red', 'white');
var cautionTextColor = Feature.color('yellow', 'white');

function getDisruptionTextColor(label) {
    if (!label) return secondaryTextColor;
    if (label.indexOf('Minor Delays') === 0 || label === 'Bus Replacements Tomorrow' || /^Bus Replacements \d/.test(label)) {
        return cautionTextColor;
    }
    return warningTextColor;
}

function getWatchDisruptionLabel(buttonIndex) {
    var labels = getCurrentDisruptionLabels(buttonIndex);
    if (labels.length === 0) {
        return null;
    }
    var rotationIndex = Math.floor(Date.now() / 3000) % labels.length;
    return labels[rotationIndex];
}

var loadingCard = new Window({
    backgroundColor: appBackgroundColor
});

if (useTextOnlySplash) {
    var splashWordmark = new Text({
        position: new Vector2(8, 54),
        size: new Vector2(screenWidth - 16, 34),
        font: 'gothic-24-bold',
        color: 'white',
        textAlign: 'center',
        text: 'PTV Notify'
    });
    loadingCard.add(splashWordmark);
} else {
    var splashImage = new Image({
        position: new Vector2((screenWidth - 120) / 2, (screenHeight - 120) / 2 - 15),
        size: new Vector2(120, 120),
        image: 'IMAGE_LOGO_SPLASH'
    });
    loadingCard.add(splashImage);
}

var loadingDetailPanelY = screenHeight - 98;
var loadingDetailPanelHeight = 44;

var loadingDetailBacking = new Rect({
    position: new Vector2(4, loadingDetailPanelY),
    size: new Vector2(screenWidth - 8, loadingDetailPanelHeight),
    backgroundColor: 'clear',
    borderColor: 'clear',
    borderWidth: 1
});
loadingCard.add(loadingDetailBacking);

var splashStatusText = new Text({
    position: new Vector2(0, screenHeight - 34),
    size: new Vector2(screenWidth, 24),
    font: 'gothic-18-bold',
    color: 'white',
    textAlign: 'center',
    text: 'Loading...'
});
loadingCard.add(splashStatusText);

var loadingDetailText = new Text({
    position: new Vector2(10, loadingDetailPanelY + 6),
    size: new Vector2(screenWidth - 20, 30),
    font: 'gothic-14-bold',
    color: 'white',
    textAlign: 'center',
    text: ''
});
loadingCard.add(loadingDetailText);

function showLoadingScreen(statusText, serverUrl) {
    loadingBaseText = statusText || 'Connecting to PTV';
    if (!loadingAnimationTimer) {
        clearLoadingTimers();
        loadingDotCount = 0;
        loadingDiagnosticToken += 1;
        setLoadingDetail('');
        loadingAnimationTimer = setInterval(function () {
            loadingDotCount = (loadingDotCount + 1) % 4;
            setLoadingHeadline();
        }, 500);
        if (serverUrl) {
            var token = loadingDiagnosticToken;
            loadingDiagnosticTimer = setTimeout(function () {
                loadingDiagnosticTimer = null;
                runLoadingDiagnostics(serverUrl, token);
            }, 5000);
        }
    }
    setLoadingHeadline();
    loadingCard.show();
}

function showMainMenu() {
    clearLoadingTimers();
    hasShownMainMenu = true;
    var items = buildMenuItems();
    mainMenu.items(0, items);
    mainMenu.selection(0, getDefaultMainMenuIndex(items));
    loadingCard.hide();
    mainMenu.show();
}

// Custom watching window with big timer
// Screen layout (144x168 Aplite): timer big at top, platform medium, route small at bottom
var watchingWindow = new Window({
    backgroundColor: appBackgroundColor,
    rippleBackground: Feature.color(true, false)
});

// Status text - small (top)
var watchingStatusText = new Text({
    position: new Vector2(0, 0),
    size: new Vector2(screenWidth, 20),
    font: 'gothic-18',
    color: 'white',
    textAlign: 'center',
    text: 'Next Service'
});
watchingWindow.add(watchingStatusText);

// Timer text - BIG monospace (top of screen)
var timerText = new Text({
    position: new Vector2(0, 18),
    size: new Vector2(screenWidth, 55),
    font: 'leco-42-numbers',
    color: 'white',
    textAlign: 'center',
    text: '...'
});
watchingWindow.add(timerText);

// Distance text - medium (between timer and platform)
var distanceText = new Text({
    position: new Vector2(0, 67),
    size: new Vector2(screenWidth, 28),
    font: 'gothic-24-bold',
    color: 'white',
    textAlign: 'center',
    text: ''
});
watchingWindow.add(distanceText);

// Platform text - medium (lower-middle)
var platformText = new Text({
    position: new Vector2(0, 95),
    size: new Vector2(screenWidth, 28),
    font: 'gothic-24-bold',
    color: 'white',
    textAlign: 'center',
    text: ''
});
watchingWindow.add(platformText);

// Route text - small (bottom, 2 lines max)
var routeText = new Text({
    position: new Vector2(5, 123),
    size: new Vector2(screenWidth - 10, 35),
    font: 'gothic-18',
    color: secondaryTextColor,
    textAlign: 'center',
    text: ''
});
watchingWindow.add(routeText);

// Up/Down arrows to indicate panel navigation
var upArrowText = new Text({
    position: new Vector2(screenWidth - 22, 2),
    size: new Vector2(20, 20),
    font: 'gothic-24-bold',
    color: 'lightGray',
    textAlign: 'right',
    text: ''
});
watchingWindow.add(upArrowText);

var downArrowText = new Text({
    position: new Vector2(screenWidth - 22, screenHeight - 32),
    size: new Vector2(20, 20),
    font: 'gothic-24-bold',
    color: 'lightGray',
    textAlign: 'right',
    text: ''
});
watchingWindow.add(downArrowText);

// Progress bar - shows seconds visually (shrinks as time passes)
var progressBar = new Rect({
    position: new Vector2(0, screenHeight - 6),
    size: new Vector2(screenWidth, 4),
    backgroundColor: 'white'
});
watchingWindow.add(progressBar);

function clearWatchTimerAnimation() {
    while (watchTimerAnimationTimers.length) {
        clearTimeout(watchTimerAnimationTimers.pop());
    }
}

function resetWatchTimerAnimationState() {
    clearWatchTimerAnimation();
    lastRenderedWatchTimerValue = null;
    lastRenderedWatchTimerMetric = null;
}

function clearWatchedSelection() {
    watchingSelectedRunRef = null;
    watchingSelectedDepartureTime = null;
}

function setWatchedSelection(dep) {
    if (!dep) {
        clearWatchedSelection();
        return;
    }

    watchingSelectedRunRef = dep.run_ref || null;
    watchingSelectedDepartureTime = dep.departure_time || null;
}

function queueWatchTimerPosition(delayMs, x, y) {
    var timerId = setTimeout(function () {
        timerText.position(new Vector2(x, y));
        for (var i = watchTimerAnimationTimers.length - 1; i >= 0; i--) {
            if (watchTimerAnimationTimers[i] === timerId) {
                watchTimerAnimationTimers.splice(i, 1);
                break;
            }
        }
    }, delayMs);
    watchTimerAnimationTimers.push(timerId);
}

function animateWatchTimerBounce(baseX, baseY) {
    clearWatchTimerAnimation();
    timerText.position(new Vector2(baseX, baseY + 1));
    queueWatchTimerPosition(70, baseX, baseY);
}

function animateWatchTimerShake(baseX, baseY) {
    clearWatchTimerAnimation();
    timerText.position(new Vector2(baseX - 2, baseY));
    queueWatchTimerPosition(45, baseX + 2, baseY);
    queueWatchTimerPosition(90, baseX - 1, baseY);
    queueWatchTimerPosition(135, baseX + 1, baseY);
    queueWatchTimerPosition(180, baseX, baseY);
}

// Back button handler for watching window
watchingWindow.on('click', 'back', function () {
    stopWatching();
});

// Up button: Show Current/Next Service (offset 0)
watchingWindow.on('click', 'up', function () {
    if (watchingDepartureOffset !== 0) {
        watchingDepartureOffset = 0;
        clearWatchedSelection();
        resetWatchTimerAnimationState();
        // Update display immediately
        var currentDep = getDepartureByOffset(watchingButtonIndex, watchingDepartureOffset);
        updateWatchingDisplay(currentDep, watchingRouteText);
        sendWatchStart(currentDep, watchingStopId);
    }
});

// Down button: Show Next Service (offset 1)
watchingWindow.on('click', 'down', function () {
    if (watchingDepartureOffset !== 1) {
        watchingDepartureOffset = 1;
        resetWatchTimerAnimationState();
        // Update display immediately
        var currentDep = getDepartureByOffset(watchingButtonIndex, watchingDepartureOffset);
        setWatchedSelection(currentDep);
        updateWatchingDisplay(currentDep, watchingRouteText);
        sendWatchStart(currentDep, watchingStopId);
    }
});
var mainMenu = new UI.Menu({
    status: false,
    backgroundColor: appBackgroundColor,
    textColor: Feature.color('white', 'white'),
    highlightBackgroundColor: Feature.color('vivid-cerulean', 'white'),
    highlightTextColor: Feature.color('black', 'black'),
    sections: [{}]
});

// Build menu items
function buildMenuItems() {
    var items = [];
    var connectionBanner = getConnectionBannerItem();

    if (connectionBanner) {
        items.push(connectionBanner);
    }

    // Station name abbreviation for Aplite menu display
    // Names are pre-abbreviated by settings.html, this just handles final length
    function abbreviateStation(name, maxLen) {
        if (!name) return '';
        maxLen = maxLen || 12; // Tight width for "Start>Dest" format

        // Names should already be abbreviated by settings.html
        // Just ensure it fits the menu display
        if (name.length <= maxLen) return name;

        // For multi-word, take first word's first 2 chars + second word
        var words = name.split(' ');
        if (words.length > 1) {
            var short = words[0].substring(0, 2) + ' ' + words[1].substring(0, maxLen - 3);
            return short.substring(0, maxLen);
        }

        // Single word - just truncate
        return name.substring(0, maxLen);
    }

    // Voice/Ask option: always show if microphone available
    // (will show setup notice if no API key is configured)
    if (Feature.microphone()) {
        items.push({
            title: 'Ask',
            subtitle: 'Voice query'
        });
    }

    // Favourite entries from settings - show as "Start→Dest" with live departure time
    // Get entry count (or migrate from legacy 3-button setup)
    var entryCount = getConfiguredEntryCount();

    for (var i = 1; i <= entryCount; i++) {
        // Try new entry naming first, fall back to legacy btn naming
        var startName = Settings.option('entry' + i + '_name') || Settings.option('btn' + i + '_name');
        var destName = Settings.option('entry' + i + '_dest_name') || Settings.option('btn' + i + '_dest_name');
        if (startName) {
            var title = abbreviateStation(startName) + '>' + abbreviateStation(destName || '?');
            // Use live departure data if available, otherwise show "Waiting..."
            var dep = getCurrentDeparture(i);
            var disruptionLabel = getCurrentDisruptionLabel(i);
            var menuDisruptionLabel = getMenuDisruptionLabel(disruptionLabel);
            var subtitle = 'Waiting...';
            if (dep) {
                var roundedMinutes = dep.minutes;

                // Recalculate precision minutes if we have departure_time
                if (dep.departure_time) {
                    var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
                    var depTime = new Date(timeStr);
                    var now = new Date();
                    var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);
                    roundedMinutes = Math.max(0, Math.floor(diffSec / 60));

                    // Unified rounding logic: >= 30s rounds UP
                    if (diffSec % 60 >= 30) {
                        roundedMinutes++;
                    }
                }

                if (roundedMinutes === 0) {
                    subtitle = 'Now';
                } else if (roundedMinutes !== null && roundedMinutes !== undefined) {
                    if (roundedMinutes >= 60) {
                        var hrs = Math.floor(roundedMinutes / 60);
                        var mins = roundedMinutes % 60;
                        subtitle = hrs + 'hr ' + mins + 'm';
                    } else {
                        subtitle = roundedMinutes + ' min';
                    }
                }
                if (menuDisruptionLabel) {
                    subtitle += ' • ' + menuDisruptionLabel;
                } else {
                    var routeType = parseInt(Settings.option('entry' + i + '_route_type') || Settings.option('btn' + i + '_route_type') || 0);

                    if (dep.platform) {
                        subtitle += ' • P' + dep.platform;
                        if (routeType === 1) {
                            subtitle += ' • Tram';
                        }
                    } else {
                        if (routeType === 1) {
                            subtitle += ' • Tram';
                        } else if (routeType === 3) {
                            subtitle += ' • V/Line';
                        } else {
                            subtitle += ' • Train';
                        }
                    }
                }
            } else if (menuDisruptionLabel) {
                subtitle = menuDisruptionLabel;
            }
            var item = { title: title, subtitle: subtitle, data: { favourite: i } };
            if (disruptionLabel) {
                item.textColor = getDisruptionTextColor(disruptionLabel);
            }
            items.push(item);
        }
    }

    // If no items at all, show setup message
    if (items.length === 0) {
        items.push({
            title: 'No entries set',
            subtitle: 'Configure in phone app'
        });
    }

    return items;
}

function getDefaultMainMenuIndex(items) {
    for (var i = 0; i < items.length; i++) {
        if (items[i].data && items[i].data.favourite) {
            return i;
        }
    }
    return 0;
}

// Menu handlers
mainMenu.on('show', function () {
    mainMenu.items(0, buildMenuItems());
});

mainMenu.on('select', function (e) {
    if (e.item.data && e.item.data.system) {
        return;
    } else if (e.item.title === 'Ask') {
        startVoiceQuery();
    } else if (e.item.data && e.item.data.favourite) {
        runFavouriteQuery(e.item.data.favourite);
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
function startVoiceQuery() {
    // Cancel any pending queries to prevent memory buildup
    cancelPendingQueries();

    // If no LLM API key, show setup notice instead of starting dictation
    var llmKey = Settings.option('llm_api_key') || '';
    if (!llmKey) {
        voiceCard.title('PTV Assistant');
        voiceCard.subtitle('');
        voiceCard.body('To use the PTV\nassistant, add an\nAnthropic API key to this\napp\'s settings.');
        voiceCard.show();
        return;
    }

    // Don't show card yet - let dictation happen on top of current view
    // This avoids window stack conflicts and memory pressure

    getVoice().dictate('start', true, function (e) {
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
var watchingDepartureOffset = 0;

function getCurrentBaseIndex(buttonIndex) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.departures || cache.departures.length === 0) {
        return -1;
    }

    var now = new Date();
    for (var i = 0; i < cache.departures.length; i++) {
        var dep = cache.departures[i];
        if (dep.departure_time) {
            var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
            var depTime = new Date(timeStr);
            if (depTime.getTime() > now.getTime() - 60000) {
                return i;
            }
        } else if (dep.minutes !== null && dep.minutes !== undefined) {
            return i;
        }
    }

    return -1;
}

// Get departure by offset (0 = next/current, 1 = service after)
function getDepartureByOffset(buttonIndex, offset) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.departures || cache.departures.length === 0) {
        return null;
    }

    var baseIndex = getCurrentBaseIndex(buttonIndex);
    if (baseIndex === -1) return null;

    var targetIndex = baseIndex + offset;
    if (targetIndex >= 0 && targetIndex < cache.departures.length) {
        return cache.departures[targetIndex];
    }
    return null;
}

function getWatchedDeparture(buttonIndex) {
    var cache = buttonDepartures[buttonIndex];
    if (!cache || !cache.departures || cache.departures.length === 0) {
        return null;
    }

    var baseIndex = getCurrentBaseIndex(buttonIndex);
    if (baseIndex === -1) {
        return null;
    }

    if (watchingDepartureOffset === 0) {
        clearWatchedSelection();
        return getDepartureByOffset(buttonIndex, 0);
    }

    var trackedIndex = -1;
    if (watchingSelectedRunRef || watchingSelectedDepartureTime) {
        for (var i = baseIndex; i < cache.departures.length; i++) {
            var dep = cache.departures[i];
            var matchesRunRef = watchingSelectedRunRef && dep.run_ref && dep.run_ref === watchingSelectedRunRef;
            var matchesDepartureTime = watchingSelectedDepartureTime && dep.departure_time && dep.departure_time === watchingSelectedDepartureTime;
            if (matchesRunRef || matchesDepartureTime) {
                trackedIndex = i;
                break;
            }
        }
    }

    if (trackedIndex !== -1) {
        var normalizedOffset = trackedIndex - baseIndex;
        if (normalizedOffset !== watchingDepartureOffset) {
            console.log('[Watch] Normalizing offset ' + watchingDepartureOffset + ' -> ' + normalizedOffset);
            watchingDepartureOffset = normalizedOffset;
        }

        var trackedDep = cache.departures[trackedIndex];
        if (watchingDepartureOffset === 0) {
            clearWatchedSelection();
        } else {
            setWatchedSelection(trackedDep);
        }
        return trackedDep;
    }

    var dep = getDepartureByOffset(buttonIndex, watchingDepartureOffset);
    if (!dep && watchingDepartureOffset > 0) {
        dep = getDepartureByOffset(buttonIndex, 0);
        if (dep) {
            console.log('[Watch] Service after became next service');
            watchingDepartureOffset = 0;
            clearWatchedSelection();
        }
    }
    if (dep && watchingDepartureOffset > 0) {
        setWatchedSelection(dep);
    }
    return dep;
}

function formatDistanceText(distanceKm) {
    if (distanceKm === null || distanceKm === undefined || isNaN(distanceKm)) {
        return null;
    }
    if (distanceKm <= 0) {
        return null;
    }
    var distText = '';
    if (distanceKm < 1.0) {
        var meters = Math.round(distanceKm * 1000);
        if (meters <= 0) {
            return null;
        }
        distText = meters + ' m away';
    } else if (distanceKm < 10.0) {
        distText = distanceKm.toFixed(1) + ' km away';
    } else {
        distText = Math.round(distanceKm) + ' km away';
    }

    return distText;
}

function sendWatchStart(dep, stopId) {
    if (!ws || !wsConnected) return;
    if (!dep || !dep.run_ref || stopId === null || stopId === undefined) {
        watchingRunRef = null;
        watchingDistanceKm = null;
        watchingDistanceRunRef = null;
        return;
    }

    if (watchingRunRef === dep.run_ref) return;

    watchingRunRef = dep.run_ref;
    watchingDistanceKm = null;
    watchingDistanceRunRef = null;
    watchingVehicleDesc = null;

    console.log('[Watch] Sending watch_start: run_ref=' + dep.run_ref + ' stop_id=' + stopId);
    ws.send(JSON.stringify({
        type: 'watch_start',
        run_ref: dep.run_ref,
        route_type: (dep.route_type !== undefined && dep.route_type !== null) ? dep.route_type : 0,
        route_id: dep.route_id,
        direction_id: dep.direction_id,
        stop_id: stopId
    }));
}

function sendWatchStop() {
    if (!ws || !wsConnected) return;
    ws.send(JSON.stringify({ type: 'watch_stop' }));
}

// Favourite query - uses cached live departure data, opens station watching mode
function runFavouriteQuery(buttonIndex) {
    // Try new entry naming first, fall back to legacy btn naming
    var name = Settings.option('entry' + buttonIndex + '_name') || Settings.option('btn' + buttonIndex + '_name');
    var destName = Settings.option('entry' + buttonIndex + '_dest_name') || Settings.option('btn' + buttonIndex + '_dest_name');
    var stopId = Settings.option('entry' + buttonIndex + '_stop_id') || Settings.option('btn' + buttonIndex + '_stop_id');

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
    watchingDepartureOffset = 0; // Reset to next train
    watchingStopId = stopId;
    watchingDistanceKm = null;
    watchingVehicleDesc = null;
    watchingRunRef = null;
    clearWatchedSelection();
    resetWatchTimerAnimationState();

    // Use cached live departure data - auto-switches between departures
    var dep = getDepartureByOffset(buttonIndex, watchingDepartureOffset);

    // Clean station names for watching window (must fit ~2 lines)
    // Names are pre-cleaned by settings.html, we just ensure they're short enough
    function shortenForWatch(n, maxLen) {
        if (!n) return '?';
        maxLen = maxLen || 14;
        // If still too long, take first 2 chars of first word + rest
        if (n.length > maxLen) {
            var words = n.split(' ');
            if (words.length > 1) {
                n = words[0].substring(0, 2) + ' ' + words.slice(1).join(' ');
            }
            if (n.length > maxLen) {
                n = n.substring(0, maxLen - 2) + '..';
            }
        }
        return n;
    }

    // Build route text for body (stored globally for favourite_update handler)
    watchingRouteText = shortenForWatch(name) + ' > ' + shortenForWatch(destName);

    if (dep && (dep.departure_time || dep.minutes !== null)) {
        // Update display with route info
        updateWatchingDisplay(dep, watchingRouteText);
        sendWatchStart(dep, watchingStopId);

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
                var currentDep = getWatchedDeparture(watchingButtonIndex);
                if (currentDep) {
                    sendWatchStart(currentDep, watchingStopId);
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
                            var extra = (diffSec % 60 >= 30) ? 1 : 0;
                            playVibrationPattern(calculateVibration(currentMinutes + extra));
                        } else if (lastVibratedMinutes !== null && currentMinutes < lastVibratedMinutes) {
                            // Minute decreased - vibrate!
                            // Minute decreased - vibrate!
                            console.log('[Watch] Minute boundary: ' + lastVibratedMinutes + ' -> ' + currentMinutes);
                            lastVibratedMinutes = currentMinutes;
                            var extra = (diffSec % 60 >= 30) ? 1 : 0;
                            playVibrationPattern(calculateVibration(currentMinutes + extra));
                        }
                    }
                } else {
                    updateWatchingDisplay(null, watchingRouteText);
                }
            }, 1000);
        }

        // Initial vibration
        if (freshMinutes !== null) {
            console.log('[Watch] Starting watch, initial minutes: ' + freshMinutes);
            lastVibratedMinutes = freshMinutes;
            lastDepartureTime = dep.departure_time;

            var extra = 0;
            if (typeof diffSec !== 'undefined' && diffSec % 60 >= 30) {
                extra = 1;
            }
            playVibrationPattern(calculateVibration(freshMinutes + extra));
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
    var disruptionLabel = watchingButtonIndex !== null ? getWatchDisruptionLabel(watchingButtonIndex) : null;
    routeText.color(disruptionLabel ? getDisruptionTextColor(disruptionLabel) : secondaryTextColor);

    if (!dep) {
        clearWatchTimerAnimation();
        timerText.position(new Vector2(0, 18));
        timerText.text('...');
        platformText.text('');
        distanceText.text('');
        routeText.text(disruptionLabel || route || '');
        progressBar.size(new Vector2(screenWidth, 4));  // Full bar when waiting
        lastRenderedWatchTimerValue = null;
        lastRenderedWatchTimerMetric = null;
        return;
    }

    var timerValue = '';
    var timerFont = 'leco-42-numbers';  // Default: monospace numbers
    var progressWidth = screenWidth;  // Default full width
    var timerMetric = null;
    var timerBaseY = 18;
    var timerBaseX = 0;

    if (dep.departure_time) {
        var timeStr = dep.departure_time.replace(/\+00:00$/, 'Z');
        var depTime = new Date(timeStr);
        var now = new Date();
        var diffSec = Math.floor((depTime.getTime() - now.getTime()) / 1000);
        timerMetric = Math.max(0, diffSec);

        if (diffSec <= 0) {
            timerValue = 'NOW!';
            timerFont = 'bitham-42-bold';  // Switch to font with letters
            progressWidth = 0;  // Empty bar when arriving
        } else {
            var totalMins = Math.floor(diffSec / 60);
            var secs = diffSec % 60;
            if (totalMins >= 60) {
                // Show H:MM:SS for 60+ minutes — use smaller font to fit on one line
                var hrs = Math.floor(totalMins / 60);
                var mins = totalMins % 60;
                timerValue = hrs + ':' + (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;
                timerFont = 'leco-36-bold-numbers';  // Smaller to fit H:MM:SS on one line
                // Progress bar: shrinks with seconds
                progressWidth = Math.floor(screenWidth * secs / 60);
            } else if (totalMins > 0) {
                timerValue = totalMins + ':' + (secs < 10 ? '0' : '') + secs;
                // Progress bar: shrinks with seconds
                progressWidth = Math.floor(screenWidth * secs / 60);
            } else {
                // Under 1 minute
                if (secs >= 30) {
                    timerValue = '0:' + secs;
                    progressWidth = Math.floor(screenWidth * secs / 60);
                } else {
                    // Under 30 seconds: show NOW!
                    timerValue = 'NOW!';
                    timerFont = 'bitham-42-bold';
                    progressWidth = 0;  // Hide progress bar for NOW!
                }
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
                timerFont = 'gothic-28-bold';  // Smaller to fit H:MM on one line
            } else {
                timerValue = dep.minutes + ' min';
            }
        }
        timerMetric = dep.minutes * 60;
        progressWidth = dep.minutes === 0 ? 0 : screenWidth;
    } else {
        timerValue = '...';
    }

    timerText.font(timerFont);
    timerText.text(timerValue);

    var distText = null;
    if (dep.route_type === 0 && dep.run_ref && dep.run_ref === watchingDistanceRunRef) {
        distText = formatDistanceText(watchingDistanceKm);
    }

    if (distText) {
        timerBaseY = 18;
        distanceText.text(distText);
    } else {
        // Recenter the timer whenever there is no usable distance text to show
        timerBaseY = 50;
        distanceText.text('');
    }
    timerText.position(new Vector2(timerBaseX, timerBaseY));

    platformText.text(dep.platform ? 'Platform ' + dep.platform : '');

    // Service name alternates with train model for metro trains
    var bottomText = disruptionLabel || route || '';
    if (!disruptionLabel && dep.route_type === 0 && watchingVehicleDesc) {
        var nowSec = Math.floor(Date.now() / 1000);
        var showAlt = (nowSec % 6) >= 3;
        if (showAlt) {
            bottomText = watchingVehicleDesc;
        }
    }
    routeText.text(bottomText);

    // Update arrow hints
    if (watchingDepartureOffset === 0) {
        upArrowText.text('');
        downArrowText.text('v');
    } else {
        upArrowText.text('^');
        downArrowText.text('');
    }

    // Update top status text
    if (watchingDepartureOffset === 0) {
        watchingStatusText.text((!wsConnected || wsConnecting) ? 'Reconnecting...' : 'Next Service');
    } else {
        watchingStatusText.text((!wsConnected || wsConnecting) ? 'Reconnecting...' : 'Service After');
    }

    progressBar.size(new Vector2(progressWidth, 4));

    if (lastRenderedWatchTimerValue !== null && timerValue !== lastRenderedWatchTimerValue &&
            lastRenderedWatchTimerMetric !== null && timerMetric !== null) {
        if (timerMetric < lastRenderedWatchTimerMetric) {
            animateWatchTimerBounce(timerBaseX, timerBaseY);
        } else if (timerMetric > lastRenderedWatchTimerMetric) {
            animateWatchTimerShake(timerBaseX, timerBaseY);
        }
    }

    lastRenderedWatchTimerValue = timerValue;
    lastRenderedWatchTimerMetric = timerMetric;
}

// Stop station watching mode
function stopWatching() {
    console.log('[Watch] Stopping watch mode');
    sendWatchStop();
    watchingButtonIndex = null;
    lastVibratedMinutes = null;
    lastDepartureTime = null;
    watchingDistanceKm = null;
    watchingVehicleDesc = null;
    watchingRunRef = null;
    clearWatchedSelection();
    watchingStopId = null;
    watchingDistanceRunRef = null;
    resetWatchTimerAnimationState();
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
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }

    if (ws && (ws.readyState === 0 || ws.readyState === 1)) {
        console.log('WebSocket connect skipped: already active');
        return;
    }

    if (wsConnecting) {
        console.log('WebSocket connect skipped: already connecting');
        return;
    }

    var serverUrl = Settings.option('server_url') || DEFAULT_SERVER_URL;

    console.log('Using server URL: ' + serverUrl);

    // Convert to WebSocket URL — no API key required for data
    var wsUrl = serverUrl
        .replace('https://', 'wss://')
        .replace('http://', 'ws://')
        .replace(/\/+$/, '') + '/ws';

    var separator = '?';
    var clientId = getOrCreateClientId();
    if (clientId) {
        wsUrl += separator + 'client_id=' + encodeURIComponent(clientId);
        separator = '&';
    }

    // Build buttons query param for instant data on connect
    // Format: "1:STOP_ID:ROUTE_TYPE:DIR_ID,2:STOP_ID:ROUTE_TYPE:DIR_ID"
    var entryCount = getConfiguredEntryCount();

    var buttonParts = [];
    for (var i = 1; i <= entryCount; i++) {
        // Try new entry naming first, fall back to legacy btn naming
        var stopId = Settings.option('entry' + i + '_stop_id') || Settings.option('btn' + i + '_stop_id');
        if (stopId) {
            var routeType = Settings.option('entry' + i + '_route_type') || Settings.option('btn' + i + '_route_type') || 0;
            var directionId = Settings.option('entry' + i + '_direction_id') || Settings.option('btn' + i + '_direction_id');
            var destId = Settings.option('entry' + i + '_dest_id') || Settings.option('btn' + i + '_dest_id');
            var part = i + ':' + stopId + ':' + routeType;
            if ((directionId !== undefined && directionId !== null) || (destId !== undefined && destId !== null)) {
                part += ':' + ((directionId !== undefined && directionId !== null) ? directionId : '');
            }
            if (destId !== undefined && destId !== null) {
                part += ':' + destId;
            }
            buttonParts.push(part);
        }
    }
    if (buttonParts.length > 0) {
        wsUrl += separator + 'buttons=' + encodeURIComponent(buttonParts.join(','));
    }

    console.log('Connecting WebSocket');

    if (!hasShownMainMenu) {
        showLoadingScreen('Connecting to PTV', serverUrl);
    }

    // Defensive cleanup
    if (ws) {
        ws.onclose = function () { };
        ws.close();
        ws = null;
    }

    wsConnecting = true;
    var socket = new WebSocket(wsUrl);
    var connectGeneration = ++wsConnectGeneration;
    ws = socket;

    socket.onopen = function () {
        if (ws !== socket || connectGeneration !== wsConnectGeneration) {
            console.log('Ignoring stale WebSocket open event');
            try {
                socket.close();
            } catch (err) { }
            return;
        }
        wsConnecting = false;
        wsConnected = true;
        console.log('WebSocket connected');
        startHeartbeat(socket, connectGeneration);
        refreshConnectionUi();
        showMainMenu();
    };

    socket.onclose = function (e) {
        if (ws === socket) {
            ws = null;
        }
        stopHeartbeat();
        wsConnecting = false;
        wsConnected = false;
        console.log('WebSocket closed, code: ' + e.code);

        // Clear stale pending requests - they won't complete anyway
        cancelPendingQueries();

        if (!hasShownMainMenu) {
            runLoadingDiagnostics(serverUrl, loadingDiagnosticToken);
        }
        refreshConnectionUi();

        // Code 4001 was previously used for invalid API key — no longer applicable
        // Server is now open access for departure data

        // Auto-reconnect for other disconnects
        if (ws === null || ws === socket) {
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connectWebSocket, 3000);
        }
    };

    socket.onerror = function (e) {
        console.log('WebSocket error');
        if (!hasShownMainMenu) {
            runLoadingDiagnostics(serverUrl, loadingDiagnosticToken);
        }
        refreshConnectionUi();
    };

    socket.onmessage = function (event) {
        if (ws !== socket || connectGeneration !== wsConnectGeneration) {
            return;
        }
        try {
            var msg = JSON.parse(event.data);

            if (msg.type === 'pong') {
                lastPongAt = Date.now();
                return;
            }

            // Handle live favourite updates (broadcast, no pending request)
            if (msg.type === 'favourite_update') {
                var updates = msg.updates || [];
                for (var i = 0; i < updates.length; i++) {
                    var u = updates[i];
                    // Store full departures array for smart switching
                    buttonDepartures[u.button_id] = {
                        departures: u.departures || [],  // Array of {minutes, platform, departure_time}
                        disruptionLabel: u.disruption_label || null,
                        disruptionLabels: u.disruption_labels || (u.disruption_label ? [u.disruption_label] : [])
                    };

                    // Station watching mode: vibrate when minutes change
                    if (watchingButtonIndex === u.button_id) {
                        var dep = getWatchedDeparture(u.button_id);
                        if (dep) {
                            sendWatchStart(dep, watchingStopId);
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
                        } else {
                            updateWatchingDisplay(null, watchingRouteText);
                        }
                    }
                }
                // Refresh menu to show updated times
                mainMenu.items(0, buildMenuItems());
                return;
            }

            if (msg.type === 'position_update') {
                if (watchingButtonIndex !== null) {
                    if (msg.distance_km !== null && msg.distance_km !== undefined) {
                        watchingDistanceKm = msg.distance_km;
                        watchingDistanceRunRef = watchingRunRef;
                    } else {
                        watchingDistanceKm = null;
                        watchingDistanceRunRef = null;
                    }
                    watchingVehicleDesc = msg.vehicle_desc || null;
                    console.log('[Watch] position_update: distance_km=' + msg.distance_km + ' vehicle_desc=' + msg.vehicle_desc);

                    var currentDep = getWatchedDeparture(watchingButtonIndex);
                    updateWatchingDisplay(currentDep, watchingRouteText);
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
    stopHeartbeat();
    wsConnecting = false;
    if (ws) {
        ws.onclose = function () { };
        ws.close();
        ws = null;
    }
    wsConnected = false;
    // Clear stale departure cache to avoid showing old data after config changes
    buttonDepartures = {};
    refreshConnectionUi();
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

    var llmApiKey = Settings.option('llm_api_key') || '';

    // Minimal payload to test if large queryHistory is causing crash
    ws.send(JSON.stringify({
        type: 'query',
        id: id,
        text: text,
        session_id: sessionId,
        llm_api_key: llmApiKey
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

// Save button config returned from the server after agent setup
function saveButtonConfig(config) {
    if (!config || !config.button_id) return;

    var entryId = config.button_id;
    console.log('Saving entry ' + entryId + ' config: ' + JSON.stringify(config));

    Settings.option('entry' + entryId + '_name', config.name || ('Entry ' + entryId));
    Settings.option('entry' + entryId + '_stop_id', config.stop_id);
    Settings.option('entry' + entryId + '_route_type', config.route_type || 0);

    // Also save dest_name if provided
    if (config.dest_name) {
        Settings.option('entry' + entryId + '_dest_name', config.dest_name);
    }
    if (config.dest_id !== undefined && config.dest_id !== null) {
        Settings.option('entry' + entryId + '_dest_id', config.dest_id);
    }

    if (config.direction_id !== undefined && config.direction_id !== null) {
        Settings.option('entry' + entryId + '_direction_id', config.direction_id);
    }

    // Update entry_count if this entry extends the count
    var currentCount = Settings.option('entry_count') || 0;
    if (entryId > currentCount) {
        Settings.option('entry_count', entryId);
    }

    // Clear stale departure cache for this entry so we don't use old data
    delete buttonDepartures[entryId];

    // Refresh menu to show new entry configuration
    mainMenu.items(0, buildMenuItems());

    // Vibrate to confirm
    Vibe.vibrate('short');
    console.log('Entry ' + entryId + ' saved successfully');

    // Reconnect WebSocket with new entries in URL to get fresh data
    // Delayed to allow LLM response panel to display first
    setTimeout(function () {
        reconnect();
    }, 500);
}

// Start the app
console.log('PTV Notify starting...');
showLoadingScreen('Connecting to PTV', Settings.option('server_url') || DEFAULT_SERVER_URL);
connectWebSocket();
