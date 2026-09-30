const assert = require('assert'), fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(require('path').join(__dirname, '../pebble/src/pkjs/pebble-js-app.js'), 'utf8');
const values = {entry_count: '10'}, sent = [];
const ctx = {console, localStorage: {getItem: key => values[key] || null},
    Pebble: {addEventListener() {}, sendAppMessage: (data, ack) => { sent.push(data); ack(); }}};
vm.createContext(ctx); vm.runInContext(source, ctx);
for (const budget of [400, 960]) {
    for (const name of ['A'.repeat(32), 'é'.repeat(32), '駅'.repeat(32), '🚋'.repeat(16), 'A;B|C\x1fD']) {
        sent.length = 0;
        for (let i = 1; i <= 10; i++) {
            values['entry' + i + '_name'] = name;
            values['entry' + i + '_dest_name'] = name;
            values['entry' + i + '_stop_id'] = String(1000 + i);
        }
        ctx.MAX_APPMSG_PAYLOAD = budget;
        ctx.syncEntriesToWatch();
        const batches = sent.filter(m => m[1] !== 6);
        assert(batches.every(m => Buffer.byteLength(m[2], 'utf8') <= budget));
        if (batches.length > 1) assert.equal(sent[0][1], 6);
        const entries = batches.flatMap(m => m[2].split('\x1f'));
        assert.equal(entries.length, 10);
        entries.forEach((entry, i) => {
            const [id, data] = entry.split('|');
            assert.equal(Number(id), i + 1);
            const fields = data.split(';');
            assert.equal(fields.length, 5);
            assert(Buffer.byteLength(fields[0]) <= 32);
            assert(Buffer.byteLength(fields[2]) <= 32);
            assert.equal(fields[1], String(1001 + i));
        });
    }
}
assert.equal(ctx.encodeDeparture({route_id: 0}).split(';')[6], '0');
console.log('Classic and colour settings byte budgets, Unicode and framing tests passed');
