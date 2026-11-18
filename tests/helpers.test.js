// Node test for the pure helpers in public/app.js:  node tests/helpers.test.js
// (The browser-only part of app.js is skipped under Node.)
const assert = require('assert'), fs = require('fs');
const { fmtLap, fmtVal, groupBy, toCSV, rate, makeCurves, curveAt, simulate } = require('../public/app.js');

assert.strictEqual(fmtLap(92.456), '1:32.456');
assert.strictEqual(fmtLap(null), '–');
assert.strictEqual(fmtVal(92.4561, 'lap_time'), '1:32.456');
assert.strictEqual(fmtVal(0.12345, 'delta'), '0.123');
assert.strictEqual(rate(5, 100), '5.0%');
assert.strictEqual(rate(0, 0), '–');
assert.deepStrictEqual([...groupBy([{a:1},{a:2},{a:1}], 'a').keys()], [1, 2]);
assert.strictEqual(toCSV([{key:'a',label:'A',unit:'s'}], [{a:'x,"y"'}]), 'A (s)\n"x,""y"""');

// interpolation + extrapolation of a degradation curve
const pts = [[1, 0], [3, 1], [5, 2]];
assert.strictEqual(curveAt(pts, 2), 0.5);
assert.strictEqual(curveAt(pts, 0), 0);
assert.ok(curveAt(pts, 7) > 2);

// the simulator on the real demo data, if it has been generated
const f = 'public/data/overall.json';
if (fs.existsSync(f)) {
  const deg = JSON.parse(fs.readFileSync(f)).charts.find(c => c.id === 'p_degradation');
  const res = simulate(makeCurves(deg.rows), 30, 22);
  assert.ok(res.length > 100 && res[0].loss <= res[1].loss);
  assert.ok(res.every(r => new Set(r.tyres).size >= 2), 'every plan must use 2+ different compounds');
  console.log('best plan on demo data:', res[0].tyres.join(' > '), 'stops', res[0].stops.join(','));
}
console.log('helpers OK');
