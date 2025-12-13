// ptv websocket client for pebble.js simple websocket client designed for ptv notify server communication

var PTVWS = function (serverUrl) {
    this.serverUrl = serverUrl;
    this.ws = null;
    this.connected = false;
    this.messageId = 0;
    this.pendingRequests = {};
    this.eventListeners = {};
    this.reconnectTimeout = null;
    this.selfDisconnect = false;
};

// check if connected
PTVWS.prototype.isConnected = function () {
    return this.connected;
};

// connect to the websocket server
PTVWS.prototype.connect = function () {
    if (this.connected) return;

    var self = this;

    // Convert HTTP URL to WebSocket URL
    var wsUrl = this.serverUrl
        .replace('https://', 'wss://')
        .replace('http://', 'ws://')
        .replace(/\/+$/, '') + '/ws';

    console.log('Connecting to: ' + wsUrl);

    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = function () {
        self.connected = true;
        self.selfDisconnect = false;
        console.log('WebSocket connected');
        self.trigger('open');
    };

    this.ws.onclose = function () {
        self.connected = false;
        console.log('WebSocket closed');
        self.trigger('close');

        // Auto-reconnect if not intentionally disconnected
        if (!self.selfDisconnect) {
            self.reconnectTimeout = setTimeout(function () {
                console.log('Attempting reconnect...');
                self.connect();
            }, 3000);
        }
    };

    this.ws.onerror = function (e) {
        console.log('WebSocket error');
        self.trigger('error', e);
    };

    this.ws.onmessage = function (event) {
        try {
            var msg = JSON.parse(event.data);
            self.handleMessage(msg);
        } catch (e) {
            console.log('Parse error: ' + e);
        }
    };
};

// disconnect from the server
PTVWS.prototype.disconnect = function () {
    this.selfDisconnect = true;

    if (this.reconnectTimeout) {
        clearTimeout(this.reconnectTimeout);
        this.reconnectTimeout = null;
    }

    if (this.ws) {
        this.ws.close();
        this.ws = null;
    }

    this.connected = false;
};

// handle incoming message
PTVWS.prototype.handleMessage = function (msg) {
    var pending = this.pendingRequests[msg.id];

    if (pending) {
        delete this.pendingRequests[msg.id];

        if (pending.timeout) {
            clearTimeout(pending.timeout);
        }

        if (msg.type === 'error') {
            if (pending.errorCb) {
                pending.errorCb({ message: msg.error || 'Unknown error' });
            }
        } else {
            if (pending.successCb) {
                pending.successCb(msg);
            }
        }
    }
};

// send a text query to the server
PTVWS.prototype.query = function (text, sessionId, queryHistory, successCb, errorCb) {
    if (!this.connected || !this.ws) {
        if (errorCb) {
            errorCb({ message: 'Not connected' });
        }
        return;
    }

    var self = this;
    var id = String(++this.messageId);

    // Store callbacks
    this.pendingRequests[id] = {
        successCb: successCb,
        errorCb: errorCb,
        timeout: setTimeout(function () {
            delete self.pendingRequests[id];
            if (errorCb) {
                errorCb({ message: 'Request timeout' });
            }
        }, 30000)
    };

    // Send message
    var message = {
        type: 'query',
        id: id,
        text: text,
        session_id: sessionId || null,
        query_history: queryHistory || []
    };

    this.ws.send(JSON.stringify(message));
};

// add event listener
PTVWS.prototype.on = function (event, callback) {
    if (!this.eventListeners[event]) {
        this.eventListeners[event] = [];
    }
    this.eventListeners[event].push(callback);
};

// trigger event
PTVWS.prototype.trigger = function (event, data) {
    var listeners = this.eventListeners[event];
    if (listeners) {
        for (var i = 0; i < listeners.length; i++) {
            listeners[i](data);
        }
    }
};

module.exports = PTVWS;
