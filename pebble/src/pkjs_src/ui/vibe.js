var simply = require('ui/simply');

var Vibe = module.exports;

var isVibrationDisabled = function() {
  return typeof window !== 'undefined' &&
    typeof window.__yarratrakIsVibrationDisabled === 'function' &&
    window.__yarratrakIsVibrationDisabled();
};

Vibe.vibrate = function (type) {
  if (isVibrationDisabled()) {
    return;
  }
  simply.impl.vibe(type);
};

Vibe.vibrateCustom = function (pattern) {
  if (isVibrationDisabled()) {
    return;
  }
  simply.impl.vibeCustom(pattern);
};

Vibe.cancel = function () {
  if (typeof simply.impl.vibeCancel === 'function') {
    simply.impl.vibeCancel();
  }
};
