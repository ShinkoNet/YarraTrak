// safe.js - building a safer world for pebble.js developers this library provides wrapper around all the asynchronous handlers

// global __loader

var safe = {};

// the name of the concatenated file to translate
safe.translateName = 'pebble-js-app.js';

safe.indent = '    ';

// translates a source line position to the originating file
safe.translatePos = function(name, lineno, colno) {
  if (name === safe.translateName) {
    var pkg = __loader.getPackageByLineno(lineno);
    if (pkg) {
      name = pkg.filename;
      lineno -= pkg.lineno;
    }
  }
  return name + ':' + lineno + ':' + colno;
};

var makeTranslateStack = function(stackLineRegExp, translateLine) {
  return function(stack, level) {
    var lines = stack.split('\n');
    var firstStackLine = -1;
    for (var i = lines.length - 1; i >= 0; --i) {
      var m = lines[i].match(stackLineRegExp);
      if (!m) {
        continue;
      }
      var line = lines[i] = translateLine.apply(this, m);
      if (line) {
        firstStackLine = i;
        if (line.indexOf(module.filename) !== -1) {
          lines.splice(i, 1);
        }
      } else {
        lines.splice(i, lines.length - i);
      }
    }
    if (firstStackLine > -1) {
      lines.splice(firstStackLine, level);
    }
    return lines;
  };
};

// translates a node style stack trace line
var translateLineV8 = function(line, msg, scope, name, lineno, colno) {
  var pos = safe.translatePos(name, lineno, colno);
  return msg + (scope ? ' ' + scope + ' (' + pos + ')' : pos);
};

// matches <msg> (<scope> '(')?
var stackLineRegExpV8 = /(.+?)(?:\s+([^\s]+)\s+\()?([^\s@:]+):(\d+):(\d+)\)?/;

safe.translateStackV8 = makeTranslateStack(stackLineRegExpV8, translateLineV8);

// translates an ios stack trace line to node style
var translateLineIOS = function(line, scope, name, lineno, colno) {
  var pos = safe.translatePos(name, lineno, colno);
  return safe.indent + 'at ' + (scope ? scope  + ' (' + pos + ')' : pos);
};

// matches (<scope> '@' )?
var stackLineRegExpIOS = /(?:([^\s@]+)@)?([^\s@:]+):(\d+):(\d+)/;

safe.translateStackIOS = makeTranslateStack(stackLineRegExpIOS, translateLineIOS);

// translates an android stack trace line to node style
var translateLineAndroid = function(line, msg, scope, name, lineno, colno) {
  if (name !== 'jskit_startup.js') {
    return translateLineV8(line, msg, scope, name, lineno, colno);
  }
};

// matches <msg> <scope> '('?
var stackLineRegExpAndroid = /^(.*?)(?:\s+([^\s]+)\s+\()?[^\s\(]*?([^\/]*?):(\d+):(\d+)\)?/;

safe.translateStackAndroid = makeTranslateStack(stackLineRegExpAndroid, translateLineAndroid);

// translates a stack trace to the originating files
safe.translateStack = function(stack, level) {
  level = level || 0;
  if (Pebble.platform === 'pypkjs') {
    return safe.translateStackV8(stack, level);
  } else if (stack.match('com.getpebble.android')) {
    return safe.translateStackAndroid(stack, level);
  } else {
    return safe.translateStackIOS(stack, level);
  }
};

var normalizeIndent = function(lines, pos) {
  pos = pos || 0;
  var label = lines[pos].match(/^[^\s]* /);
  if (label) {
    var indent = label[0].replace(/./g, ' ');
    for (var i = pos + 1, ii = lines.length; i < ii; i++) {
      lines[i] = lines[i].replace(/^\t/, indent);
    }
  }
  return lines;
};

safe.translateError = function(err, intro, level) {
  var name = err.name;
  var message = err.message || err.toString();
  var stack = err.stack;
  var result = [intro || 'JavaScript Error:'];
  if (message && (!stack || stack.indexOf(message) === -1)) {
    if (name && message.indexOf(name + ':') === -1) {
      message = name + ': ' + message;
    }
    result.push(message);
  }
  if (stack) {
    Array.prototype.push.apply(result, safe.translateStack(stack, level));
  }
  return normalizeIndent(result, 1).join('\n');
};

// dumps error messages to the console
safe.dumpError = function(err, intro, level) {
  if (typeof err === 'object') {
    console.log(safe.translateError(err, intro, level));
  } else {
    console.log('Error: dumpError argument is not an object');
  }
};

// logs runtime warnings to the console
safe.warn = function(message, level, name) {
  var err = new Error(message);
  err.name = name || 'Warning';
  safe.dumpError(err, 'Warning:', level);
};

// takes a function and return a new function with a call to it wrapped in a try/catch statement
safe.protect = function(fn) {
  return fn ? function() {
    try {
      fn.apply(this, arguments);
    } catch (err) {
      safe.dumpError(err);
    }
  } : undefined;
};

// wrap event handlers added by pebble.addeventlistener
var pblAddEventListener = Pebble.addEventListener;
Pebble.addEventListener = function(eventName, eventCallback) {
  pblAddEventListener.call(this, eventName, safe.protect(eventCallback));
};

var pblSendMessage = Pebble.sendAppMessage;
Pebble.sendAppMessage = function(message, success, failure) {
  return pblSendMessage.call(this, message, safe.protect(success), safe.protect(failure));
};

// wrap settimeout and setinterval
var originalSetTimeout = setTimeout;
window.setTimeout = function(callback, delay) {
  if (safe.warnSetTimeoutNotFunction !== false && typeof callback !== 'function') {
    safe.warn('setTimeout was called with a `' + (typeof callback) + '` type. ' +
              'Did you mean to pass a function?');
    safe.warnSetTimeoutNotFunction = false;
  }
  return originalSetTimeout(safe.protect(callback), delay);
};

var originalSetInterval = setInterval;
window.setInterval = function(callback, delay) {
  if (safe.warnSetIntervalNotFunction !== false && typeof callback !== 'function') {
    safe.warn('setInterval was called with a `' + (typeof callback) + '` type. ' +
              'Did you mean to pass a function?');
    safe.warnSetIntervalNotFunction = false;
  }
  return originalSetInterval(safe.protect(callback), delay);
};

// wrap the geolocation api callbacks
var watchPosition = navigator.geolocation.watchPosition;
navigator.geolocation.watchPosition = function(success, error, options) {
  return watchPosition.call(this, safe.protect(success), safe.protect(error), options);
};

var getCurrentPosition = navigator.geolocation.getCurrentPosition;
navigator.geolocation.getCurrentPosition = function(success, error, options) {
  return getCurrentPosition.call(this, safe.protect(success), safe.protect(error), options);
};

var ajax;

// try to load the ajax library if available and silently fail if it is not found
try {
  ajax = require('ajax');
} catch (err) {}

// wrap the success and failure callback of the ajax library
if (ajax) {
  ajax.onHandler = function(eventName, callback) {
    return safe.protect(callback);
  };
}

module.exports = safe;
