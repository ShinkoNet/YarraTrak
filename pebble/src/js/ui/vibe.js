var simply = require('ui/simply');

var Vibe = module.exports;

Vibe.vibrate = function (type) {
  simply.impl.vibe(type);
};

Vibe.vibrateCustom = function (pattern) {
  simply.impl.vibeCustom(pattern);
};
