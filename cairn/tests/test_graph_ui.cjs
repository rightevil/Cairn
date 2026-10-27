const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
let data=[],layouts=0,zoom=1,pan={x:9,y:7},fit=[];
const graph={json(value){if(value){data=value.elements;return;}return {elements:{nodes:data.filter(e=>!e.data.source),edges:data.filter(e=>e.data.source)}};},layout(){return {run(){layouts++;}};},zoom(v){if(v!==undefined)zoom=v;return zoom;},pan(v){if(v!==undefined)pan=v;return pan;},on(){},resize(){},destroy(){},nodes(){return data.filter(e=>!e.data.source).map(e=>({data:key=>e.data[key]}));},fit(nodes){fit=nodes;},center(){}};
const context=vm.createContext({URLSearchParams,setTimeout,clearTimeout,document:{documentElement:{}},getComputedStyle:()=>({getPropertyValue:()=> '#888888'}),cytoscape:options=>{data=options.elements;return graph;},ResizeObserver:class{observe(){}disconnect(){}}});
const root=path.join(__dirname,'../src/cairn/server/static');
for(const name of ['agent-output.js','development.js','design.js','workspace.js'])vm.runInContext(fs.readFileSync(path.join(root,name),'utf8'),context);
const app=context.cairnApp();app.$nextTick=fn=>fn();app.$refs={blackboard:{clientWidth:1000}};app.view='graph';app.sideTab='development';app.boardTab='graph';
app.project={facts:[{id:'origin',description:'Baseline'},{id:'old',description:'Historical evidence'}],intents:[
 {id:'a',description:'Core',from:['origin'],work:{key:'A',state:'verified',dependencies:[],purpose:'implementation'}},
 {id:'b',description:'UI',from:[],work:{key:'B',state:'running',dependencies:['a'],purpose:'implementation'}},
 {id:'c',description:'Check',from:[],work:{key:'C',state:'ready',dependencies:['b'],purpose:'implementation'}},
 {id:'design',description:'Old design',work:{state:'verified',purpose:'design'}}]};
assert(app.graphStateLabel(app.project.intents[2]).includes('依赖等待'));
app.updateGraph();assert(!data.some(e=>e.data.ref==='old'||e.data.ref==='design'));
assert(data.some(e=>e.data.type==='dependency'&&e.data.source==='i:a'&&e.data.target==='i:b'));
assert(data.some(e=>e.data.type==='fact'));
zoom=1.2;pan={x:13,y:5};app.project.intents[1].work.state='reviewing';app.updateGraph();assert.equal(layouts,0);assert.equal(zoom,1.2);assert.equal(pan.x,13);
app.focusRunning();assert.equal(fit.length,1);assert.equal(fit[0].data('ref'),'b');
app.taskFilter='waiting';assert.equal(app.visibleWork().length,1);assert.equal(app.visibleWork()[0].id,'c');
app.taskFilter='all';app.graphFocus='b';assert(app.graphElements().some(e=>e.data.ref==='c'));
app.graphHistory=true;assert(app.graphElements().some(e=>e.data.ref==='old'));
console.log('Graph checks passed: state text, dependency/fact semantics, history filtering, current activity and viewport preservation');

app.project.intents[1].work.state='running';app.project.intents[2].work.state='running';app.graphFocus='';app.taskFilter='all';app.updateGraph();app.showToast=()=>{};
app.graphRunCursor=-1;zoom=3;app.focusRunning();assert.equal(fit[0].data('ref'),'c');assert.equal(zoom,1.25);
app.focusRunning();assert.equal(fit[0].data('ref'),'b');
