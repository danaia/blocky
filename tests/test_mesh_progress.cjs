const {test} = require('node:test');
const assert = require('node:assert/strict');
const {meshProgress} = require('../scripts/mesh_progress.js');

const job = {status:'running', started_at:1000, asset_ids:['a','b'], steps:[
  {key:'mesh_batch',status:'running',updated_at:1050}
]};
const assets = [
  {asset_id:'a',status:'mesh_complete',mesh_exists:true,updated_at:new Date(1140000).toISOString()},
  {asset_id:'b',status:'image_complete',mesh_exists:false}
];
test('counts only selected, existing completed meshes and estimates the current model', () => {
  const p=meshProgress(job,[...assets,{asset_id:'unrelated',status:'mesh_complete',mesh_exists:true}],1185,90);
  assert.equal(p.completed,1); assert.equal(p.total,2);
  assert.equal(p.stage,'Generating model 2 of 2');
  assert.equal(p.percent,67); assert.equal(p.remaining,85);
});
test('overdue work never claims 100% or a zero ETA', () => {
  const p=meshProgress(job,assets,1400,90);
  assert.equal(p.remaining,null); assert.ok(p.percent<85);
});
test('prep shows the active stage and includes remaining model work in ETA', () => {
  const p=meshProgress({...job,status:'preparing',steps:[
    {key:'stop_comfyui',status:'done',updated_at:1000},
    {key:'wait_vram',status:'running',updated_at:1005}
  ]},[],1010,90);
  assert.equal(p.stage,'Waiting for GPU memory'); assert.ok(p.percent<15);
  assert.ok(p.remaining>=180);
});
test('completed meshes still wait for cleanup and Blender', () => {
  const ready=assets.map(a=>({...a,status:'mesh_complete',mesh_exists:true}));
  const p=meshProgress(job,ready,1185,90);
  assert.equal(p.percent,85); assert.equal(p.remaining,40);
  assert.match(p.stage,/Finalizing/);
});
test('Blender export stays below 100 until the job succeeds', () => {
  const exporting={...job,steps:[...job.steps,{key:'game_assets',status:'running',updated_at:1200}]};
  assert.match(meshProgress(exporting,assets,1210).stage,/Blender/);
  assert.ok(meshProgress(exporting,assets,1500).percent<100);
  const done=meshProgress({...exporting,status:'succeeded',ended_at:1220},assets,1500);
  assert.equal(done.percent,100);assert.equal(done.elapsed,220);assert.equal(done.remaining,0);
});
test('failure and cancellation stop the timer and clear the ETA', () => {
  for(const status of ['failed','cancelled']) {
    const p=meshProgress({...job,status,ended_at:1200},assets,1800);
    assert.equal(p.elapsed,200);assert.equal(p.remaining,null);assert.equal(p.terminal,true);
    assert.ok(p.percent<100);
  }
});
test('stale missing files do not count as ready', () => {
  const p=meshProgress(job,[{...assets[0],mesh_exists:false},assets[1]],1100);
  assert.equal(p.completed,0);
});
