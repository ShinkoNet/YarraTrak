const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(require('path').join(__dirname, '../pebble/src/pkjs/pebble-js-app.js'), 'utf8');
let success, failure, request;
const messages = [];
const updates = [];
let scheduled;
function XHR() { request = this; }
XHR.prototype.open = function(method, url) { this.method = method; this.url = url; };
XHR.prototype.setRequestHeader = function() {};
XHR.prototype.send = function(body) { this.body = JSON.parse(body); };
XHR.prototype.abort = function() { this.aborted = true; };
const ctx = {navigator: {geolocation: {getCurrentPosition(ok, fail) { success = ok; failure = fail; }}},
    XMLHttpRequest: XHR, getServerUrl: () => 'https://example.test', getOrCreateClientId: () => 'test',
    IN_NEARBY_ROW: 15, IN_NEARBY_STATUS: 16, IN_NEARBY_TRACK: 17,
    setTimeout: fn => { scheduled = fn; return 1; }, clearTimeout: () => { scheduled = null; },
    handleFavUpdate: msg => updates.push(msg), sendToWatch: (type, text) => messages.push({type, text})};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('var nearbyGeneration ='), source.indexOf('function parsePipe(')), ctx);
ctx.findNearbyDepartures('1');
failure({code: 1});
assert.equal(messages.pop().text, '1|error|Allow phone location access');
ctx.findNearbyDepartures('2');
const stale = success;
ctx.findNearbyDepartures('3');
stale({coords: {latitude: 1, longitude: 1}});
assert.equal(request, undefined);
success({coords: {latitude: -37.81, longitude: 144.96}});
assert.equal(request.method, 'POST');
assert.equal(request.body.latitude, -37.81);
request.status = 200;
request.responseText = JSON.stringify({departures: Array.from({length: 20}, (_, i) => ({route_type: 1, distance_m: 30,
    stop_id: 10, route_id: 5, direction_id: 0,
    departure_time: '2026-10-01T00:00:00Z', stop_name: 'Stop|name', destination: 'Destination', route_number: '5'}))});
request.onload();
assert.equal(messages.filter(m => m.type === 15).length, 16);
assert.equal(messages.pop().text, '3|done|16');
assert(messages.filter(m => m.type === 15).every(m => m.text.split('|').length === 8 && m.text.length < 200));
ctx.startNearbyTracker('3|0');
assert.equal(messages.pop().text, '3|0|10|1|5|0');
assert.equal(request.body.direction_id, 0);
assert.equal(request.url, 'https://example.test/api/v1/nearby/tracker');
request.status = 200;
request.responseText = JSON.stringify({departures: [{run_ref: 'next'}]});
request.onload();
assert.equal(updates[0].updates[0].button_id, 255);
assert(scheduled);
scheduled();
const oldRequest = request;
ctx.stopNearbyTracker();
assert(oldRequest.aborted);
oldRequest.onload();
assert.equal(updates.length, 1);
assert.equal(scheduled, null);
ctx.findNearbyDepartures('4');
success({coords: {latitude: 0, longitude: 0}});
request.ontimeout();
assert.equal(messages.pop().text, '4|error|Request timed out');
console.log('Nearby location, stale request, payload and error tests passed');
