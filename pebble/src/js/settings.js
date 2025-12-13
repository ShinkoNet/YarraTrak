/**
 * Settings Module for Pebble.js
 * 
 * Handles persistent settings storage and configuration page integration.
 */

var Settings = module.exports;

var state = {
    options: {},
    listeners: []
};

var STORAGE_KEY = 'ptv_settings';

/**
 * Initialize settings from localStorage
 */
Settings.init = function () {
    try {
        var saved = localStorage.getItem(STORAGE_KEY);
        if (saved) {
            state.options = JSON.parse(saved);
        }
    } catch (e) {
        console.log('Settings load error: ' + e);
        state.options = {};
    }

    // Register Pebble event handlers
    Pebble.addEventListener('showConfiguration', Settings.onOpenConfig);
    Pebble.addEventListener('webviewclosed', Settings.onCloseConfig);
};

/**
 * Save settings to localStorage
 */
Settings.save = function () {
    try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state.options));
    } catch (e) {
        console.log('Settings save error: ' + e);
    }
};

/**
 * Get/set a single option
 */
Settings.option = function (key, value) {
    if (arguments.length === 0) {
        return state.options;
    }

    if (arguments.length === 1) {
        return state.options[key];
    }

    if (value === undefined || value === null) {
        delete state.options[key];
    } else {
        state.options[key] = value;
    }

    Settings.save();
    return value;
};

/**
 * Configure settings page URL and callbacks
 */
Settings.config = function (options, openCallback, closeCallback) {
    if (typeof options === 'string') {
        options = { url: options };
    }

    state.listeners.push({
        params: options,
        open: openCallback,
        close: closeCallback
    });
};

/**
 * Handle opening configuration page
 */
Settings.onOpenConfig = function (e) {
    var listener = state.listeners[state.listeners.length - 1];
    if (!listener || !listener.params || !listener.params.url) {
        console.log('No config URL defined');
        return;
    }

    // Build URL with current settings as hash
    var url = listener.params.url;
    url += '#' + encodeURIComponent(JSON.stringify(state.options));

    // Open the settings page
    Pebble.openURL(url);

    if (listener.open) {
        listener.open(e);
    }
};

/**
 * Handle closing configuration page
 */
Settings.onCloseConfig = function (e) {
    if (!e.response || e.response === 'CANCELLED') {
        return;
    }

    var listener = state.listeners[state.listeners.length - 1];

    // Parse returned options
    var newOptions = {};
    try {
        newOptions = JSON.parse(decodeURIComponent(e.response));
    } catch (ex) {
        console.log('Config parse error: ' + ex);
        return;
    }

    // Merge new options
    for (var key in newOptions) {
        if (newOptions.hasOwnProperty(key)) {
            state.options[key] = newOptions[key];
        }
    }

    Settings.save();

    // Call the close callback
    if (listener && listener.close) {
        listener.close({
            options: newOptions
        });
    }
};

// Initialize on load
Settings.init();
