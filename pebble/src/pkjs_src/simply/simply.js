// simply.js provides the classic "simplyjs" api on top of pebblejs

var WindowStack = require('ui/windowstack');
var Card = require('ui/card');
var Vibe = require('ui/vibe');

var simply = {};

simply.text = function(textDef) {
  var wind = WindowStack.top();
  if (!wind || !(wind instanceof Card)) {
    wind = new Card(textDef);
    wind.show();
  } else {
    wind.prop(textDef, true);
  }
};

// vibrates the pebble
simply.vibe = function(type) {
  return Vibe.vibrate(type);
};

module.exports = simply;
