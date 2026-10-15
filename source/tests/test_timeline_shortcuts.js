// Exercises the actual browser event handler with a controlled DOM boundary.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../pipeline_core/static/timeline.js'), 'utf8');
const handler = source.slice(source.indexOf('function keyboardCommand(e){'), source.indexOf("document.addEventListener('keydown',keyboardCommand)"));
const calls = [];
const controls = new Map();
const context = {P: {}, pending: false, drag: null, playhead: 30, zoom: 2,
  $: id => {if (!controls.has(id)) controls.set(id, {open: false, click: () => calls.push(id)}); return controls.get(id);},
  tell: message => calls.push(message), render: () => calls.push('render'),
  cancelDrag: () => calls.push('cancel'), current: () => ({start: 20}),
  selectedCommand: (op, args) => calls.push([op, args]), setPlayhead: frame => calls.push(['playhead', frame])};
vm.createContext(context); vm.runInContext(handler, context);
function press(key, extras = {}) {
  calls.length = 0; let prevented = false;
  context.keyboardCommand({key, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
    repeat: false, target: {tagName: 'DIV', isContentEditable: false, closest: () => null},
    preventDefault: () => {prevented = true;}, ...extras});
  return {calls: JSON.parse(JSON.stringify(calls)), prevented};
}
const bindings = {b:'split',d:'duplicate',z:'undo',y:'redo',Enter:'draft','0':'fit'};
for (const modifier of ['metaKey','ctrlKey']) for (const [key, action] of Object.entries(bindings)) {
  assert.deepEqual(press(key,{[modifier]:true}), {calls:[action],prevented:true});
}
assert.deepEqual(press('z',{ctrlKey:true,shiftKey:true}).calls,['redo']);
for (const [key, action] of Object.entries({s:'split',m:'marker',i:'markIn',o:'markOut','?':'shortcuts',' ':'livePlay',Delete:'lift',Backspace:'lift'})) assert.deepEqual(press(key).calls,[action]);
for (const key of ['Delete','Backspace']) assert.deepEqual(press(key,{shiftKey:true}).calls,['rippleDelete']);
assert.deepEqual(press('ArrowRight').calls,[['playhead',31]]);
assert.deepEqual(press('ArrowLeft',{shiftKey:true}).calls,[['playhead',20]]);
assert.deepEqual(press('ArrowRight',{altKey:true,shiftKey:true}).calls,[['move',{start:30,magnetic:false}]]);
for (const tagName of ['INPUT','TEXTAREA','SELECT','VIDEO','AUDIO']) {
  assert.deepEqual(press('b',{ctrlKey:true,target:{tagName}}),{calls:[],prevented:false});
}
assert.deepEqual(press('s',{target:{tagName:'DIV',isContentEditable:true}}).calls,[]);
assert.deepEqual(press(' ',{target:{tagName:'BUTTON',closest:()=>({})}}).calls,[]);
assert.deepEqual(press('s',{repeat:true}).calls,[]);
context.pending=true; assert.match(press('s').calls[0],/Wait/); context.pending=false;
context.$('shortcutHelp').open=true; assert.deepEqual(press('s').calls,[]); context.$('shortcutHelp').open=false;
context.drag={}; assert.deepEqual(press('Escape').calls,['cancel']); context.drag=null;
press('+',{metaKey:true}); assert.equal(context.zoom,2.5);
press('-',{ctrlKey:true}); assert.equal(context.zoom,2);
context.P=null; assert.deepEqual(press('s').calls,[]);
console.log('Keyboard shortcut regression checks passed.');
