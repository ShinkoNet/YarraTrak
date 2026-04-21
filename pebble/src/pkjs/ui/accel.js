var Emitter = require('emitter');

var Accel = new Emitter();

module.exports = Accel;

var WindowStack = require('ui/windowstack');
var Window = require('ui/window');
var simply = require('ui/simply');

var state;

Accel.init = function() {
  if (state) {
    Accel.off();
  }

  state = Accel.state = {
    rate: 100,
    samples: 25,
    subscribe: false,
    subscribeMode: 'auto',
    listeners: [],
  };
};

Accel.onAddHandler = function(type, subtype) {
  if (type === 'data') {
    Accel.autoSubscribe();
  }
};

Accel.onRemoveHandler = function(type, subtype) {
  if (!type || type === 'accelData') {
    Accel.autoSubscribe();
  }
};

var accelDataListenerCount = function() {
  var count = Accel.listenerCount('data');
  var wind = WindowStack.top();
  if (wind) {
    count += wind.listenerCount('accelData');
  }
  return count;
};

Accel.autoSubscribe = function() {
  if (state.subscribeMode !== 'auto') { return; }
  var subscribe = (accelDataListenerCount() > 0);
  if (subscribe !== state.subscribe) {
    return Accel.config(subscribe, true);
  }
};

// the accelerometer configuration parameter for {@link simply.accelconfig}

// changes the accelerometer configuration
Accel.config = function(opt, auto) {
  if (arguments.length === 0) {
    return {
      rate: state.rate,
      samples: state.samples,
      subscribe: state.subscribe,
    };
  } else if (typeof opt === 'boolean') {
    opt = { subscribe: opt };
  }
  for (var k in opt) {
    if (k === 'subscribe') {
      state.subscribeMode = opt[k] && !auto ? 'manual' : 'auto';
    }
    state[k] = opt[k];
  }
  return simply.impl.accelConfig(Accel.config());
};

// peeks at the current accelerometer values
Accel.peek = function(callback) {
  if (state.subscribe) {
    throw new Error('Cannot use accelPeek when listening to accelData events');
  }
  return simply.impl.accelPeek.apply(this, arguments);
};

// simply.js accel tap event

Accel.emitAccelTap = function(axis, direction) {
  var e = {
    axis: axis,
    direction: direction,
  };
  if (Window.emit('accelTap', axis, e) === false) {
    return false;
  }
  Accel.emit('tap', axis, e);
};

// simply.js accel data point

// simply.js accel data event

Accel.emitAccelData = function(accels, callback) {
  var e = {
    samples: accels.length,
    accel: accels[0],
    accels: accels,
  };
  if (callback) {
    return callback(e);
  }
  if (Window.emit('accelData', null, e) === false) {
    return false;
  }
  Accel.emit('data', e);
};

Accel.init();
