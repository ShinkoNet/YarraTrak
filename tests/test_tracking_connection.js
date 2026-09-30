const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(require('path').join(__dirname, '../pebble/src/pkjs/pebble-js-app.js'), 'utf8');
const events = {}, sockets = [], messages = [];
function Socket() { this.sent = []; sockets.push(this); }
Socket.prototype.send = function(value) { this.sent.push(JSON.parse(value)); };
Socket.prototype.close = function() {};
const ctx = {console: {log() {}},
    localStorage: {getItem: key => key === 'client_id' ? 'test' : null, setItem() {}, removeItem() {}},
    Pebble: {addEventListener: (name, fn) => { events[name] = fn; },
        sendAppMessage: (data, ack) => { messages.push(data); ack(); }},
    WebSocket: Socket, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}};
vm.createContext(ctx);
vm.runInContext(source, ctx);
ctx.connect(); sockets[0].onopen();
events.appmessage({payload: {3: 2, 4: '1|123|1071|0|1|0'}});
assert.equal(sockets[0].sent[0].run_ref, '123');
sockets[0].onclose({code: 1006});
assert.equal(messages[messages.length - 1][2], '||');
ctx.connect(); sockets[1].onopen();
assert.equal(sockets[1].sent[0].type, 'watch_start');
assert.equal(sockets[1].sent[0].run_ref, '123');
events.appmessage({payload: {3: 2, 4: '1|456|1071|0|1|0'}});
let count = messages.length;
ctx.handlePositionUpdate({run_ref: '123', distance_km: 9});
assert.equal(messages.length, count);
ctx.handlePositionUpdate({run_ref: '456', distance_km: 1.5});
assert.equal(messages[messages.length - 1][2], '150||456');
events.appmessage({payload: {3: 3, 4: ''}});
sockets[1].onclose({code: 1006});
ctx.connect(); sockets[2].onopen();
assert.equal(sockets[2].sent.length, 0);
console.log('Tracking reconnect, stop and stale-position tests passed');
