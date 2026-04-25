/**
 * YarraTrak PKJS companion.
 *
 * Owns the WebSocket bridge, heartbeat, config page, and compacts server JSON
 * into AppMessage dictionaries for the native C watch app. The watch owns the
 * UI; this file owns only transport.
 */

var DEFAULT_SERVER_URL = 'https://ptv.netcavy.net';
var CONFIG_URL_PATH = '/pebble-config.html';
var PING_INTERVAL_MS = 25 * 1000;
var PONG_TIMEOUT_MS = 70 * 1000;
var RECONNECT_MIN_MS = 2 * 1000;
var RECONNECT_MAX_MS = 60 * 1000;

// AppMessage key IDs (must match pebble/appinfo.json appKeys).
var KEY_INBOUND_TYPE = 1;
var KEY_INBOUND_DATA = 2;
var KEY_OUTBOUND_TYPE = 3;
var KEY_OUTBOUND_DATA = 4;

// Inbound (JS -> C) types.
var IN_CONN_STATE      = 1;
var IN_FAV_UPDATE      = 2;
var IN_POSITION_UPDATE = 3;
var IN_FLAGS_SYNC      = 4;
var IN_ENTRY_SYNC      = 5;
var IN_CLEAR_ENTRIES   = 6;
var IN_ENTRY_SYNC_BULK = 8;
var IN_QUERY_RESULT    = 9;
var IN_QUERY_CLARIFY   = 10;
var IN_QUERY_ERROR     = 11;
var IN_QUERY_SAVED     = 12;
var IN_ENTRY_SYNC_REPLACE = 13;

// Outbound (C -> JS) types.
var OUT_READY       = 1;
var OUT_WATCH_START = 2;
var OUT_WATCH_STOP  = 3;
var OUT_OPEN_CONFIG = 4;
var OUT_REFRESH     = 5;
var OUT_QUERY       = 6;

// Memory caps — the watch inbox is 1024 bytes; leave 64 bytes for the
// AppMessage key/type overhead so bloated LLM text can never OOM the watch.
// The watch's card body buffer is 512 bytes, so cap further for the result
// path to sidestep mid-word truncation on its display as well.
var MAX_APPMSG_PAYLOAD  = 960;
var MAX_TTS_TEXT        = 480;
var MAX_CLARIFY_OPTIONS = 8;
var MAX_CLARIFY_LABEL   = 30;
var MAX_CLARIFY_VALUE   = 46;
var MAX_CLARIFY_QUESTION = 60;

// Connection states.
var CONN_OFFLINE    = 0;
var CONN_CONNECTING = 1;
var CONN_CONNECTED  = 2;

// ---- Settings ----------------------------------------------------------

var DEMO_ENTRIES = [
    { name: 'Flinders St', full_name: 'Flinders Street Station', stop_id: 1071,
      dest_name: 'Belgrave', full_dest_name: 'Belgrave Station', dest_id: 1018,
      route_type: 0, direction_id: 2 },
    { name: 'Caulfield', full_name: 'Caulfield Station', stop_id: 1036,
      dest_name: 'Town Hall', full_dest_name: 'Town Hall Station', dest_id: 1235,
      route_type: 0, direction_id: 1 },
    { name: 'Barkly Sq/Syd Rd', full_name: 'Barkly Square/Sydney Rd #20', stop_id: 2811,
      dest_name: 'QVM/Eliz St', full_dest_name: 'Queen Victoria Market/Elizabeth St #7', dest_id: 2258,
      route_type: 1, direction_id: 11 },
    { name: 'Sth Cross', full_name: 'Southern Cross Railway Station', stop_id: 1181,
      dest_name: 'Bendigo', full_dest_name: 'Bendigo Railway Station', dest_id: 1509,
      route_type: 3, direction_id: 6 }
];

function lsGet(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
}
function lsSet(key, value) {
    try {
        if (value === null || value === undefined) {
            localStorage.removeItem(key);
        } else {
            localStorage.setItem(key, String(value));
        }
    } catch (e) { }
}

function getOption(name) { return lsGet(name); }
function setOption(name, value) { lsSet(name, value); }

function boolOption(name) {
    var v = getOption(name);
    return v === 'true' || v === '1' || v === true;
}

// AI defaults OFF when no value is stored — out-of-the-box installs get
// the assistant disabled until the user explicitly enables it in settings.
// boolOption() can't express "default true" because a missing key returns
// null, which it treats as false. Centralise the default here so the bit-
// packing and the runtime check (startQuery) agree.
function aiAssistantDisabled() {
    var v = getOption('disable_ai_assistant');
    if (v === null || v === undefined || v === '') return true;
    return v === 'true' || v === '1' || v === true;
}

function getOrCreateClientId() {
    var id = getOption('client_id');
    if (!id) {
        id = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
            var r = Math.random() * 16 | 0;
            var v = c === 'x' ? r : (r & 0x3 | 0x8);
            return v.toString(16);
        });
        setOption('client_id', id);
    }
    return id;
}

function getServerUrl() {
    if (!boolOption('enable_third_party_endpoint')) return DEFAULT_SERVER_URL;
    return getOption('server_url') || DEFAULT_SERVER_URL;
}

function getConfiguredEntryCount() {
    var v = parseInt(getOption('entry_count') || '0', 10);
    return isFinite(v) ? v : 0;
}

// One-shot upgrade path. Two historical storage shapes exist, both covered
// here, no-ops when there's nothing to migrate:
//
//  1. Pebble.js Settings (master JS build) serialised every Settings.option()
//     call into a single JSON blob at `options:<uuid>`. Raw `entry1_stop_id`
//     / `btn1_stop_id` never existed as top-level localStorage keys — so the
//     old migration checked the wrong place, missed every real JS install,
//     fell through to firstLaunchDemoSeed, and the user saw demo favourites
//     replace their real ones on first launch after the update.
//
//  2. Pre-Settings-module V1 builds stored favourites under btnN_* keys
//     directly. Fold those into entryN_* the same way.
//
// Both paths write into the new entryN_* raw-key layout and set entry_count
// so the demo seed skips.
var APP_UUID = '61ae3254-ce00-49db-aad8-23143f649b91';

function migrateLegacyBtnKeys() {
    if (getOption('entry_count') || getOption('entry1_stop_id')) return;

    var fields = ['name', 'full_name', 'stop_id', 'dest_name', 'full_dest_name',
                  'dest_id', 'route_type', 'direction_id'];

    // Pebble.js Settings blob — single JSON object under `options:<uuid>`.
    var blobKey = 'options:' + APP_UUID;
    var blobRaw = lsGet(blobKey);
    if (blobRaw) {
        var blob = null;
        try { blob = JSON.parse(blobRaw); } catch (e) { blob = null; }
        if (blob && typeof blob === 'object') {
            var migrated = 0;
            for (var i = 1; i <= 10; i++) {
                // Accept either the canonical entryN_* form or the older
                // btnN_* naming the JS build also understood as input.
                var stopId = blob['entry' + i + '_stop_id'] || blob['btn' + i + '_stop_id'];
                if (!stopId) continue;
                for (var f = 0; f < fields.length; f++) {
                    var v = blob['entry' + i + '_' + fields[f]];
                    if (v === undefined || v === null || v === '') {
                        v = blob['btn' + i + '_' + fields[f]];
                    }
                    if (v !== undefined && v !== null && v !== '') {
                        setOption('entry' + i + '_' + fields[f], v);
                    }
                }
                migrated = i;
            }
            var flagKeys = ['server_url', 'llm_api_key', 'use_24hr_time',
                            'disable_ai_assistant', 'enable_third_party_endpoint',
                            'disable_vibration', 'disable_animations',
                            'disable_distance_info', 'dark_theme', 'client_id'];
            for (var k = 0; k < flagKeys.length; k++) {
                var fv = blob[flagKeys[k]];
                if (fv !== undefined && fv !== null && fv !== '') {
                    setOption(flagKeys[k], fv);
                }
            }
            if (migrated > 0) {
                setOption('entry_count', blob.entry_count || migrated);
                console.log('Migrated ' + migrated + ' favourites from Pebble.js Settings blob');
            }
            // Drop the old blob so we don't re-run this next boot.
            lsSet(blobKey, null);
            if (migrated > 0) return;
        }
    }

    // Raw btnN_* fallback — some ancient local installs never went through
    // the Settings module.
    if (!getOption('btn1_stop_id')) return;
    var migratedRaw = 0;
    for (var j = 1; j <= 10; j++) {
        if (!getOption('btn' + j + '_stop_id')) continue;
        for (var ff = 0; ff < fields.length; ff++) {
            var rv = getOption('btn' + j + '_' + fields[ff]);
            if (rv !== null) setOption('entry' + j + '_' + fields[ff], rv);
            setOption('btn' + j + '_' + fields[ff], null);
        }
        migratedRaw = j;
    }
    if (migratedRaw > 0) {
        setOption('entry_count', migratedRaw);
        console.log('Migrated ' + migratedRaw + ' legacy btn* favourites to entry*');
    }
}

function firstLaunchDemoSeed() {
    if (getOption('entry_count') || getOption('entry1_stop_id')) return;
    console.log('First launch: seeding demo entries');
    setOption('entry_count', DEMO_ENTRIES.length);
    for (var i = 0; i < DEMO_ENTRIES.length; i++) {
        var d = DEMO_ENTRIES[i];
        var n = i + 1;
        setOption('entry' + n + '_name', d.name);
        setOption('entry' + n + '_full_name', d.full_name);
        setOption('entry' + n + '_stop_id', d.stop_id);
        setOption('entry' + n + '_dest_name', d.dest_name);
        setOption('entry' + n + '_full_dest_name', d.full_dest_name);
        setOption('entry' + n + '_dest_id', d.dest_id);
        setOption('entry' + n + '_route_type', d.route_type);
        setOption('entry' + n + '_direction_id', d.direction_id);
    }
}

function collectSettingsSnapshot() {
    var snapshot = {};
    var keys = ['server_url', 'llm_api_key', 'use_24hr_time', 'disable_ai_assistant',
                'enable_third_party_endpoint', 'disable_vibration',
                'disable_animations', 'disable_distance_info', 'dark_theme',
                'bg_fx', 'entry_count', 'client_id'];
    for (var i = 0; i < keys.length; i++) {
        var v = getOption(keys[i]);
        if (v !== null) snapshot[keys[i]] = v;
    }
    var count = getConfiguredEntryCount();
    for (var j = 1; j <= count; j++) {
        var fields = ['name', 'full_name', 'stop_id', 'dest_name', 'full_dest_name',
                      'dest_id', 'route_type', 'direction_id'];
        for (var k = 0; k < fields.length; k++) {
            var kk = 'entry' + j + '_' + fields[k];
            var vv = getOption(kk);
            if (vv !== null) snapshot[kk] = vv;
        }
    }
    return snapshot;
}

function applySettingsPayload(payload) {
    if (!payload || typeof payload !== 'object') return;
    // The config page's save payload is the authoritative view of the
    // user's favourites — anything not mentioned in it should not remain
    // on disk. Clear every entry slot up front, then reapply whatever the
    // payload contains. This keeps the previous "nuke > newCount" logic
    // working while also closing edge cases where entry_count in the
    // payload doesn't match reality (e.g. the user adds an empty slot
    // and saves, leaving a stale slot below the declared count).
    var fields = ['name', 'full_name', 'stop_id', 'dest_name', 'full_dest_name',
                  'dest_id', 'route_type', 'direction_id'];
    for (var i = 1; i <= 10; i++) {
        for (var k = 0; k < fields.length; k++) {
            setOption('entry' + i + '_' + fields[k], null);
        }
    }
    for (var key in payload) {
        if (!Object.prototype.hasOwnProperty.call(payload, key)) continue;
        setOption(key, payload[key]);
    }
}

// ---- AppMessage send queue ---------------------------------------------

var outboxQueue = [];  // each item = { dict, retries }
var sending = false;
var MAX_RETRIES = 2;

function enqueueSend(dict) {
    outboxQueue.push({ dict: dict, retries: 0 });
    drainQueue();
}

function drainQueue() {
    if (sending || outboxQueue.length === 0) return;
    sending = true;
    var item = outboxQueue.shift();
    Pebble.sendAppMessage(item.dict, function ack() {
        sending = false;
        drainQueue();
    }, function nack(e) {
        var msg = e && e.error && e.error.message;
        console.log('AppMessage NACK: ' + msg);
        if (item.retries < MAX_RETRIES) {
            item.retries++;
            outboxQueue.unshift(item);
            setTimeout(function () { sending = false; drainQueue(); },
                       200 * item.retries);
        } else {
            sending = false;
            drainQueue();
        }
    });
}

function sendToWatch(type, data) {
    var d = {};
    d[KEY_INBOUND_TYPE] = type;
    d[KEY_INBOUND_DATA] = data == null ? '' : String(data);
    enqueueSend(d);
}

function sendConnState(state, message) {
    // Extended format: "state|message" — the C side splits on '|' and uses
    // the message (if any) as the splash subtitle, giving users a specific
    // reason when the first connect doesn't land.
    var payload = String(state);
    if (message) payload += '|' + String(message).slice(0, 60);
    sendToWatch(IN_CONN_STATE, payload);
}

// One-shot startup diagnostic: only fires after we've spent ~8 seconds
// unable to reach the WebSocket since boot, and only once per session.
// Cheap — one GET /api/v1/health with a 5 s timeout — so it doesn't pay
// unless something's already wrong.
var diagnosticRan = false;
function runStartupDiagnosticIfStillOffline() {
    if (diagnosticRan || wsConnected) return;
    diagnosticRan = true;

    var base = getServerUrl().replace(/\/+$/, '');
    var url = base + '/api/v1/health';
    try {
        var xhr = new XMLHttpRequest();
        xhr.timeout = 5000;
        xhr.open('GET', url);
        xhr.onload = function () {
            if (wsConnected) return;
            if (xhr.status >= 200 && xhr.status < 300) {
                sendConnState(CONN_CONNECTING, 'Slow connection');
            } else {
                sendConnState(CONN_OFFLINE, 'Server error ' + xhr.status);
            }
        };
        xhr.onerror = function () {
            if (!wsConnected) sendConnState(CONN_OFFLINE, 'Phone offline');
        };
        xhr.ontimeout = function () {
            if (!wsConnected) sendConnState(CONN_OFFLINE, 'Server unreachable');
        };
        xhr.send();
    } catch (e) {
        sendConnState(CONN_OFFLINE, 'Network error');
    }
}

// ---- Settings sync to watch ---------------------------------------------

function syncFlagsToWatch() {
    var bits = 0;
    // Bit layout matches handle_flags_sync in pebble/src/c/protocol.c:
    //   1  disable_vibration
    //   2  disable_animations    (combined; was split between fx + shake)
    //   4  disable_distance_info (was disable_timer_shake)
    //   8  disable_ai_assistant  (DEFAULTS TRUE — AI is off out-of-the-box)
    //   16 use_24hr_time
    //   32 dark_theme
    if (boolOption('disable_vibration'))      bits |= 1;
    if (boolOption('disable_animations'))     bits |= 2;
    if (boolOption('disable_distance_info'))  bits |= 4;
    if (aiAssistantDisabled())                bits |= 8;
    if (boolOption('use_24hr_time'))          bits |= 16;
    if (boolOption('dark_theme'))             bits |= 32;
    // Pipe-append the bg-fx enum (0=rings, 1=starfield, 2=plasma, 3=fire,
    // 4=cube). Older watch builds ignore the trailing token.
    var bg = parseInt(getOption('bg_fx') || '0', 10);
    if (isNaN(bg) || bg < 0 || bg > 4) bg = 0;
    sendToWatch(IN_FLAGS_SYNC, String(bits) + '|' + String(bg));
}

function syncEntriesToWatch() {
    var count = getConfiguredEntryCount();
    var chunks = [];
    for (var i = 1; i <= count; i++) {
        var stopId = getOption('entry' + i + '_stop_id');
        if (!stopId) continue;
        // Only the fields the watch needs. full_name / full_dest_name /
        // dest_id stay on the phone for the config page; the watch renders
        // menu rows from `name` + `dest_name` and issues watch_start from
        // stop_id/route_type/direction_id (+ run_ref from the departure).
        var fields = [
            getOption('entry' + i + '_name') || '',
            stopId || '0',
            getOption('entry' + i + '_dest_name') || '',
            getOption('entry' + i + '_route_type') || '0',
            getOption('entry' + i + '_direction_id') || '0'
        ];
        chunks.push(i + '|' + fields.join(';'));
    }

    // No favourites: an explicit clear is the only correct signal.
    if (chunks.length === 0) {
        sendToWatch(IN_CLEAR_ENTRIES, '');
        return;
    }

    // Batch into as few AppMessages as possible — each send costs ~150-300ms
    // of phone/watch roundtrip. Watch inbox is 1024 bytes; leave 64 bytes
    // of headroom for keys/type/overhead.
    var INBOX_BUDGET = 960;
    var SEP = '\x1f';  // unit separator
    var joined = chunks.join(SEP);

    if (joined.length <= INBOX_BUDGET) {
        // Happy path: the entire entry set fits in one AppMessage. Skip the
        // preceding IN_CLEAR_ENTRIES round-trip (~150-300 ms saved) and let
        // the watch do an atomic clear+apply inside the REPLACE handler.
        sendToWatch(IN_ENTRY_SYNC_REPLACE, joined);
        return;
    }

    // Multi-chunk fallback: the CLEAR still matters because a BULK chunk
    // can't self-authoritatively clear without wiping peer chunks. The
    // watch's 500 ms post-clear debounce absorbs the inter-message gap so
    // users don't see the empty state between clear and first chunk.
    sendToWatch(IN_CLEAR_ENTRIES, '');
    var batch = '';
    for (var j = 0; j < chunks.length; j++) {
        var piece = chunks[j];
        var candidate = batch.length ? (batch + SEP + piece) : piece;
        if (candidate.length > INBOX_BUDGET && batch.length > 0) {
            sendToWatch(IN_ENTRY_SYNC_BULK, batch);
            batch = piece;
        } else {
            batch = candidate;
        }
    }
    if (batch.length > 0) {
        sendToWatch(IN_ENTRY_SYNC_BULK, batch);
    }
}

function syncAllToWatch() {
    syncFlagsToWatch();
    syncEntriesToWatch();
}

// ---- WebSocket ----------------------------------------------------------

var ws = null;
var wsGen = 0;
var wsConnected = false;
var pingTimer = null;
var pongWatchdog = null;
var reconnectTimer = null;
var reconnectDelay = RECONNECT_MIN_MS;
var watchingRunRef = null;  // reconciles against server position_update stream

function buildWsUrl() {
    var base = getServerUrl()
        .replace(/^https:/, 'wss:')
        .replace(/^http:/, 'ws:')
        .replace(/\/+$/, '');
    var url = base + '/ws';
    var clientId = getOrCreateClientId();
    var params = [];
    params.push('client_id=' + encodeURIComponent(clientId));

    var count = getConfiguredEntryCount();
    var parts = [];
    for (var i = 1; i <= count; i++) {
        var stopId = getOption('entry' + i + '_stop_id');
        if (!stopId) continue;
        var routeType = getOption('entry' + i + '_route_type') || '0';
        var directionId = getOption('entry' + i + '_direction_id') || '';
        var destId = getOption('entry' + i + '_dest_id') || '';
        var part = i + ':' + stopId + ':' + routeType;
        if (directionId !== '' || destId !== '') part += ':' + directionId;
        if (destId !== '') part += ':' + destId;
        parts.push(part);
    }
    if (parts.length > 0) {
        params.push('buttons=' + encodeURIComponent(parts.join(',')));
    }
    return url + '?' + params.join('&');
}

function clearTimers() {
    if (pingTimer)    { clearInterval(pingTimer);   pingTimer = null; }
    if (pongWatchdog) { clearTimeout(pongWatchdog); pongWatchdog = null; }
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
        reconnectTimer = null;
        connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

function startHeartbeat(socket, gen) {
    clearTimers();
    pingTimer = setInterval(function () {
        if (ws !== socket || wsGen !== gen) return;
        try { socket.send(JSON.stringify({ type: 'ping' })); } catch (e) { }
    }, PING_INTERVAL_MS);

    pongWatchdog = setTimeout(function () {
        if (ws !== socket || wsGen !== gen) return;
        console.log('Pong watchdog fired; reconnecting');
        try { socket.close(); } catch (e) { }
    }, PONG_TIMEOUT_MS);
}

function armPongWatchdog(socket, gen) {
    if (pongWatchdog) clearTimeout(pongWatchdog);
    pongWatchdog = setTimeout(function () {
        if (ws !== socket || wsGen !== gen) return;
        try { socket.close(); } catch (e) { }
    }, PONG_TIMEOUT_MS);
}

function parseIsoToUnix(str) {
    if (!str) return 0;
    var s = String(str).replace(/\+00:00$/, 'Z');
    var t = Date.parse(s);
    return isFinite(t) ? Math.floor(t / 1000) : 0;
}

function encodeDeparture(d) {
    if (!d) return '';
    var mins = (d.minutes !== null && d.minutes !== undefined) ? d.minutes : '';
    var dep_unix = parseIsoToUnix(d.departure_time);
    var fields = [
        mins === '' ? '' : String(mins),
        dep_unix ? String(dep_unix) : '',
        d.route_type !== undefined && d.route_type !== null ? String(d.route_type) : '',
        d.direction_id !== undefined && d.direction_id !== null ? String(d.direction_id) : '',
        d.run_ref || '',
        d.platform || '',
        d.route_id || ''
    ];
    return fields.join(';');
}

// Drop duplicate run_refs and collapse services
function dedupeDepartures(deps) {
    if (!deps || deps.length === 0) return [];
    var seenRef = {};
    var seenMinute = {};
    var out = [];
    for (var i = 0; i < deps.length; i++) {
        var d = deps[i];
        if (!d) continue;
        var refKey = d.run_ref || ('idx_' + i);
        if (seenRef[refKey]) continue;
        // "YYYY-MM-DDTHH:MM" — truncating to the minute catches both exact
        // matches and sub-second siblings (16:13:00 / 16:13:45).
        var minute = (d.departure_time || '').slice(0, 16);
        if (minute && seenMinute[minute]) continue;
        seenRef[refKey] = true;
        if (minute) seenMinute[minute] = true;
        out.push(d);
    }
    return out;
}

function handleFavUpdate(msg) {
    var updates = msg.updates || [];
    for (var i = 0; i < updates.length; i++) {
        var u = updates[i];
        var bid = u.button_id;
        var deps = dedupeDepartures(u.departures);
        // Cache three: dep[0] = now, dep[1] = service-after, dep[2] = buffer
        // for auto-advance when the current service passes. Matches V1.
        var dep1 = encodeDeparture(deps[0]);
        var dep2 = encodeDeparture(deps[1]);
        var dep3 = encodeDeparture(deps[2]);
        var labels = u.disruption_labels || (u.disruption_label ? [u.disruption_label] : []);
        // 0x1E (record separator) between labels — matches the C decoder.
        var labelStr = labels.join('\x1e');
        var payload = bid + '|' + dep1 + '|' + dep2 + '|' + dep3 + '|' + labelStr;
        sendToWatch(IN_FAV_UPDATE, payload);
    }
}

function handlePositionUpdate(msg) {
    var d_km = (msg.distance_km !== null && msg.distance_km !== undefined)
        ? Math.round(msg.distance_km * 100) : '';
    var veh = msg.vehicle_desc || '';
    var payload = d_km + '|' + veh + '|' + (watchingRunRef || '');
    sendToWatch(IN_POSITION_UPDATE, payload);
}

function connect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) {
        try { ws.onclose = null; ws.close(); } catch (e) { }
        ws = null;
    }

    sendConnState(CONN_CONNECTING);
    var url = buildWsUrl();
    console.log('WebSocket connecting: ' + url);

    var gen = ++wsGen;
    var socket;
    try {
        socket = new WebSocket(url);
    } catch (e) {
        console.log('WebSocket construct failed: ' + e);
        scheduleReconnect();
        return;
    }
    ws = socket;

    socket.onopen = function () {
        if (ws !== socket || wsGen !== gen) return;
        console.log('WebSocket connected');
        wsConnected = true;
        reconnectDelay = RECONNECT_MIN_MS;
        sendConnState(CONN_CONNECTED);
        startHeartbeat(socket, gen);
    };

    socket.onmessage = function (event) {
        if (ws !== socket || wsGen !== gen) return;
        try {
            var msg = JSON.parse(event.data);
            if (msg.type === 'pong') {
                armPongWatchdog(socket, gen);
                return;
            }
            if (msg.type === 'favourite_update') {
                handleFavUpdate(msg);
                return;
            }
            if (msg.type === 'position_update') {
                handlePositionUpdate(msg);
                return;
            }
            // Everything else with an id → an AI query response / error /
            // clarification. The server also attaches learned_stop and
            // entry_config on the same frame; handleQueryMessage pulls
            // those out before dispatching to the watch card.
            if (msg.id != null && pendingQueries[msg.id]) {
                handleQueryMessage(msg);
                return;
            }
        } catch (e) {
            console.log('WebSocket parse error: ' + e);
        }
    };

    socket.onerror = function (e) {
        console.log('WebSocket error: ' + (e && e.message));
    };

    socket.onclose = function (e) {
        if (ws !== socket) return;
        console.log('WebSocket closed (code=' + (e && e.code) + ')');
        wsConnected = false;
        ws = null;
        clearTimers();
        sendConnState(CONN_OFFLINE);
        scheduleReconnect();
    };
}

function wsSend(obj) {
    if (!ws || !wsConnected) return;
    try { ws.send(JSON.stringify(obj)); } catch (e) { console.log('ws.send failed: ' + e); }
}

// ---- AI query transport -----------------------------------------------

var nextQueryId = 0;
var pendingQueries = {};  // id -> { timeout, text }
var sessionId = null;

// Rolling query history: last N successful queries + their learned stops.
// Forwarded with every new query so the agent can lean on recency when
// ranking clarifications ("Richmond" is probably the same one as last time).
// Each entry: { text, stop_name?, stop_id?, route_type?, at }
var QUERY_HISTORY_MAX = 5;
var queryHistory = [];

function loadQueryHistory() {
    try {
        var s = lsGet('query_history');
        if (!s) return;
        var parsed = JSON.parse(s);
        if (Array.isArray(parsed)) queryHistory = parsed.slice(0, QUERY_HISTORY_MAX);
    } catch (e) { queryHistory = []; }
}

function saveQueryHistory() {
    try { lsSet('query_history', JSON.stringify(queryHistory.slice(0, QUERY_HISTORY_MAX))); }
    catch (e) { }
}

function recordQuerySuccess(text, learnedStop) {
    var entry = {
        text: (text || '').slice(0, 160),
        at: Math.floor(Date.now() / 1000),
    };
    if (learnedStop) {
        if (learnedStop.stop_name) entry.stop_name = String(learnedStop.stop_name).slice(0, 60);
        if (learnedStop.stop_id)   entry.stop_id = learnedStop.stop_id;
        if (learnedStop.route_type !== undefined) entry.route_type = learnedStop.route_type;
    }
    queryHistory.unshift(entry);
    if (queryHistory.length > QUERY_HISTORY_MAX) {
        queryHistory.length = QUERY_HISTORY_MAX;
    }
    saveQueryHistory();
}

function queryInFlight() {
    for (var _ in pendingQueries) return true;
    return false;
}

function cancelPendingQueries() {
    for (var id in pendingQueries) {
        if (pendingQueries[id].timeout) clearTimeout(pendingQueries[id].timeout);
    }
    pendingQueries = {};
}

function truncate(str, max) {
    if (typeof str !== 'string') return '';
    if (str.length <= max) return str;
    return str.slice(0, max - 1);  // leave room for safety; NUL added on C side
}

function startQuery(text) {
    if (aiAssistantDisabled()) {
        sendToWatch(IN_QUERY_ERROR, 'AI assistant disabled in settings');
        return;
    }
    if (!ws || !wsConnected) {
        sendToWatch(IN_QUERY_ERROR, 'Not connected');
        return;
    }
    var llmKey = getOption('llm_api_key') || '';
    if (!llmKey) {
        sendToWatch(IN_QUERY_ERROR,
            'Add an OpenRouter API key in the phone app settings to use Ask.');
        return;
    }

    cancelPendingQueries();

    var id = String(++nextQueryId);
    var queryText = truncate(String(text || ''), 400);
    pendingQueries[id] = {
        text: queryText,
        timeout: setTimeout(function () {
            if (pendingQueries[id]) {
                delete pendingQueries[id];
                sendToWatch(IN_QUERY_ERROR, 'Timed out after 30s');
            }
        }, 30000)
    };

    wsSend({
        type: 'query',
        id: id,
        text: queryText,
        session_id: sessionId,
        llm_api_key: llmKey,
        query_history: queryHistory,
    });
}

function handleQueryMessage(msg) {
    var pending = pendingQueries[msg.id];
    if (!pending) return;
    if (pending.timeout) clearTimeout(pending.timeout);
    delete pendingQueries[msg.id];

    if (msg.session_id) sessionId = msg.session_id;
    if (msg.entry_config) saveEntryConfig(msg.entry_config);

    if (msg.type === 'error') {
        sendToWatch(IN_QUERY_ERROR,
            truncate(msg.error || 'Unknown error', MAX_TTS_TEXT));
        return;
    }

    var data = msg.data || msg;
    var payload = data && data.payload;
    var kind = data && data.type;

    if (kind === 'RESULT' && payload) {
        // Successful terminal — record the query + the stop the agent locked
        // onto so the next query can hint context to the LLM.
        recordQuerySuccess(pending.text, msg.learned_stop);
        var text = payload.tts_text || 'No info';
        sendToWatch(IN_QUERY_RESULT, truncate(text, MAX_TTS_TEXT));
    } else if (kind === 'CLARIFICATION' && payload) {
        var question = truncate(payload.question_text || 'Please clarify',
                                MAX_CLARIFY_QUESTION);
        var opts = (payload.options || []).slice(0, MAX_CLARIFY_OPTIONS);
        var encoded = [question];
        for (var i = 0; i < opts.length; i++) {
            var label = truncate(opts[i].label || '', MAX_CLARIFY_LABEL);
            var value = truncate(opts[i].value || label, MAX_CLARIFY_VALUE);
            if (!label) continue;
            encoded.push(label + '\x1f' + value);
        }
        var payloadStr = encoded.join('\x1e');
        if (payloadStr.length > MAX_APPMSG_PAYLOAD) {
            payloadStr = payloadStr.slice(0, MAX_APPMSG_PAYLOAD);
        }
        sendToWatch(IN_QUERY_CLARIFY, payloadStr);
    } else {
        sendToWatch(IN_QUERY_ERROR, 'Unsupported response');
    }
}

function saveEntryConfig(config) {
    if (!config) return;
    // Accept legacy `button_id` from older server builds for one transition cycle.
    var rawId = config.entry_id != null ? config.entry_id : config.button_id;
    if (rawId == null) return;
    var id = parseInt(rawId, 10);
    if (!(id >= 1 && id <= 10)) return;
    setOption('entry' + id + '_name', config.name || ('Entry ' + id));
    setOption('entry' + id + '_stop_id', config.stop_id);
    setOption('entry' + id + '_route_type', config.route_type || 0);
    if (config.dest_name) setOption('entry' + id + '_dest_name', config.dest_name);
    if (config.dest_id != null) setOption('entry' + id + '_dest_id', config.dest_id);
    if (config.direction_id != null) setOption('entry' + id + '_direction_id', config.direction_id);

    var currentCount = getConfiguredEntryCount();
    if (id > currentCount) setOption('entry_count', id);
    syncEntriesToWatch();
    sendToWatch(IN_QUERY_SAVED, String(id));
}

// ---- Outbound message handlers from watch ------------------------------

function parsePipe(str) { return String(str == null ? '' : str).split('|'); }

Pebble.addEventListener('ready', function () {
    console.log('PKJS ready');
    migrateLegacyBtnKeys();
    firstLaunchDemoSeed();
    loadQueryHistory();
    getOrCreateClientId();
    sendConnState(CONN_CONNECTING);
    syncAllToWatch();
    connect();
    setTimeout(runStartupDiagnosticIfStillOffline, 8000);
});

Pebble.addEventListener('appmessage', function (e) {
    var p = e.payload || {};
    var type = p[KEY_OUTBOUND_TYPE];
    var data = p[KEY_OUTBOUND_DATA] || '';
    switch (type) {
        case OUT_READY:
            // Watch just booted / reopened; resend settings and current conn state.
            syncAllToWatch();
            sendConnState(wsConnected ? CONN_CONNECTED : CONN_CONNECTING);
            break;
        case OUT_WATCH_START: {
            // button_id|run_ref|stop_id|route_type|route_id|direction_id
            var f = parsePipe(data);
            var run_ref = f[1] || '';
            var stop_id = parseInt(f[2] || '0', 10);
            var route_type = parseInt(f[3] || '0', 10);
            var route_id = f[4] || '';
            var direction_id = parseInt(f[5] || '0', 10);
            watchingRunRef = run_ref;
            wsSend({ type: 'watch_start', run_ref: run_ref, stop_id: stop_id,
                     route_type: route_type, route_id: route_id,
                     direction_id: direction_id });
            break;
        }
        case OUT_WATCH_STOP:
            watchingRunRef = null;
            wsSend({ type: 'watch_stop' });
            break;
        case OUT_OPEN_CONFIG:
            openConfigPage();
            break;
        case OUT_REFRESH:
            if (wsConnected) {
                // Force a fresh snapshot by bouncing the socket.
                try { ws.close(); } catch (err) { }
            } else {
                connect();
            }
            break;
        case OUT_QUERY:
            startQuery(data);
            break;
    }
});

function openConfigPage() {
    var base = DEFAULT_SERVER_URL;  // Config page always on default server.
    var snapshot = collectSettingsSnapshot();
    // Expose the watch platform so settings.html can gate colour-only
    // effects (plasma / fire) behind the capability check. Falls through
    // gracefully if Pebble.getActiveWatchInfo isn't available on the host.
    try {
        if (typeof Pebble.getActiveWatchInfo === 'function') {
            var info = Pebble.getActiveWatchInfo();
            if (info && info.platform) snapshot._platform = info.platform;
        }
    } catch (err) { /* best effort */ }
    var url = base + CONFIG_URL_PATH + '#' + encodeURIComponent(JSON.stringify(snapshot));
    Pebble.openURL(url);
}

Pebble.addEventListener('showConfiguration', openConfigPage);

Pebble.addEventListener('webviewclosed', function (e) {
    if (!e || !e.response) return;
    var data;
    try {
        data = JSON.parse(decodeURIComponent(e.response));
    } catch (err) {
        try { data = JSON.parse(e.response); } catch (err2) { return; }
    }
    if (!data) return;
    console.log('Config closed; saving options');
    applySettingsPayload(data);
    syncAllToWatch();
    // Bounce the socket so server re-issues favourite_update with new entries.
    if (ws) { try { ws.close(); } catch (err) { } }
    else connect();
});
