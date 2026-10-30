const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '../src/cairn/server/static');
const context = vm.createContext({ URLSearchParams, setTimeout, clearTimeout });
for (const file of ['vendor/dagre.min.js', 'vendor/cytoscape.min.js', 'vendor/cytoscape-dagre.js', 'development.js', 'design.js', 'workspace.js']) {
  vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), context);
}
const app = context.cairnApp();
const task = (id, state, dependencies = []) => ({ id, description: 'Task ' + id, from: ['origin'],
  work: { key: id, role: 'implementer', state, dependencies, head_commit: 'candidate' } });
app.project = { facts: [{ id: 'origin', description: 'Baseline' }], intents: [
  task('a', 'verified'), task('b', 'reviewing', ['a']), task('c', 'ready', ['b']),
  { id: 'review', description: 'Review B', work: { role: 'reviewer', state: 'running', subject: 'b', dependencies: [] } },
] };
app.developmentNow = Date.now();
app.developmentState.runs = [{ id: 'run-review', intent_id: 'review', role: 'reviewer', state: 'running',
  worker: 'independent-reviewer', heartbeat_at: new Date().toISOString(), pid: 42 }];
const card = id => app.graphElements().find(e => e.data.ref === id).data;
assert.equal(card('b').live, true, 'A collapsed reviewer must mark the implementation task as live');
assert(card('b').activity.includes('independent-reviewer'));
assert.equal(card('b').steps[1].active, true);
assert(!app.graphElements().some(e => e.data.ref === 'review'), 'Auxiliary reviews stay collapsed by default');
assert.equal(card('c').tone, 'waiting');
assert(card('c').stateLabel.includes('依赖等待'));
app.project.intents[1].work.result = { pending_design: true };
assert.equal(card('b').tone, 'waiting');
assert(card('b').stateLabel.includes('设计决策'));
assert(card('b').steps.every(s => !s.done && !s.active), 'A design gap is not completed implementation');
app.project.intents[1].work.result = {};
app.project.intents.push(task('resource', 'ready'), task('retry', 'ready'), task('failed', 'failed'));
app.developmentState.scheduling = {
  resource: { status: 'resources', detail: 'Shared resource is held' },
  retry: { status: 'retry', detail: 'Retry scheduled' },
};
assert.equal(card('resource').activity, 'Shared resource is held');
assert.equal(card('retry').stateLabel, '! 等待重试');
app.taskFilter = 'waiting';
assert.deepEqual(Array.from(app.visibleWork(), i => i.id).sort(), ['c', 'failed', 'resource', 'retry']);
app.taskFilter = 'all';
app.graphHistory = true;
assert(app.graphElements().some(e => e.data.ref === 'review'));
assert(app.graphElements().some(e => e.data.type === 'review' && e.data.source === 'i:b' && e.data.target === 'i:review'));
app.project.intents[3].work.state = 'verified';
app.project.intents[3].work.result = { output: { verdict: 'request_changes' } };
assert.equal(card('review').tone, 'waiting');
assert.equal(card('review').stateLabel, '◇ 需修订', 'A completed review is not a passing implementation');
app.graphHistory = false;
app.project.intents[1].work.state = 'merged';
assert.equal(app.projectProgress().verified, 1, 'Merge alone must not advance verified progress');
assert.equal(card('b').steps[3].done, false);
assert.equal(card('b').steps[3].active, true);

// Exercise real Cytoscape/Dagre positions rather than a mock that cannot move nodes.
context.elements = app.graphElements();
vm.runInContext(`workspaceGraph = cytoscape({ headless:true, styleEnabled:true, elements,
  layout:{name:'dagre',rankDir:'LR'}, minZoom:.1, maxZoom:2.5 });`, context);
const graph = vm.runInContext('workspaceGraph', context);
app.view = 'graph'; app.sideTab = 'development';
app.$refs = { blackboard: { clientWidth: 800, getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }) } };
app.$nextTick = fn => fn();
graph.zoom(.65); graph.pan(vm.runInContext('({ x: 110, y: 70 })', context));
const position = { ...graph.getElementById('i:b').position() };
app.project.intents[1].work.state = 'verified';
app.updateGraph();
assert.deepEqual({ ...graph.getElementById('i:b').position() }, position, 'Polling must retain actual node positions');
assert.equal(graph.zoom(), .65); assert.equal(graph.pan().x, 110);
assert.equal(app.graphCards.find(c => c.ref === 'b').tone, 'verified');
assert.equal(app.graphZoom, 65);
assert.equal(app.graphTransform, 'translate(110px,70px) scale(0.65)');
app.selection = { kind: 'intent', id: 'b' }; app.panelOpen = true;
app.syncGraphSelection();
assert(graph.getElementById('i:b').hasClass('related'));
assert(graph.edges('[source="i:b"][target="i:c"]').hasClass('related'));
app.panelOpen = false; app.syncGraphSelection();
assert.equal(graph.elements('.related').length, 0);
app.project.intents.push(task('new', 'ready', ['b'])); app.updateGraph();
assert.equal(graph.zoom(), .65); assert.equal(graph.pan().y, 70);
assert(app.graphCards.some(c => c.ref === 'new'));
app.zoomGraph(100); assert.equal(graph.zoom(), 2.5);
app.zoomGraph(.001); assert.equal(graph.zoom(), .1);
app.graphFocus = 'b'; app.taskFilter = 'waiting'; app.fitGraph();
assert.equal(app.graphFocus, ''); assert.equal(app.taskFilter, 'all');
app.destroyGraph();
assert.equal(app.graphCards.length, 0);
assert.equal(app.graphZoom, 100);
console.log('Progress graph checks passed: task/review aggregation, truthful stages, waiting filters, real layout, polling, selection and zoom');
