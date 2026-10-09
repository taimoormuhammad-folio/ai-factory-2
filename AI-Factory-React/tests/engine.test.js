import test from 'node:test';
import assert from 'node:assert/strict';
import {initialRun,runReducer,overallProgress,validateFiles} from '../src/engine.js';

function approveGates(state, indexes = [1, 2, 4, 9]) {
  let next = state;
  for (const index of indexes) {
    if (next.statuses?.[index] === 'awaiting') next = runReducer(next, { type: 'approve', index });
  }
  return next;
}

test('automatic demo visits every stage and ends at 100 percent',()=>{
  let s={...initialRun};
  const visited=new Set();
  for(let i=0;i<2500&&!s.complete;i++){
    visited.add(s.active);
    s=approveGates(runReducer(s,{type:'tick',amount:4}));
  }
  assert.deepEqual([...visited],[0,1,2,3,4,5,6,7,8,9]);
  assert.equal(s.complete,true);
  assert.equal(overallProgress(s),100);
  assert.equal(s.approval,'approved');
  assert.deepEqual(runReducer(s,{type:'tick',amount:10}),s);
});

test('pause prevents progress; resume continues',()=>{
  const paused=runReducer(initialRun,{type:'pause'});
  assert.deepEqual(runReducer(paused,{type:'tick',amount:50}),paused);
  assert.equal(runReducer(runReducer(paused,{type:'pause'}),{type:'tick',amount:50}).progress,50);
});

test('final app approval finishes the run and revision reruns frontend and backend',()=>{
  const smoke={...initialRun,active:9,progress:100,statuses:Array.from({length:10},(_,i)=>i<9?'done':'awaiting'),progresses:Array.from({length:10},(_,i)=>i<9?100:100)};
  const approved=runReducer(smoke,{type:'approve',index:9});
  assert.equal(approved.complete,true);
  assert.equal(approved.active,9);
  const revision=runReducer({...smoke,statuses:smoke.statuses.slice(),progresses:smoke.progresses.slice()},{type:'revise'});
  assert.equal(revision.active,5);
  assert.equal(revision.statuses[6],'active');
  assert.equal(revision.revision,1);
  assert.equal(revision.progress,0);
  assert.equal(revision.paused,false);
  assert.deepEqual(runReducer(initialRun,{type:'approve'}),initialRun);
});

test('uploads reject invalid, empty and oversized files',()=>{
  const r=validateFiles([{name:'brief.pdf',size:100},{name:'photo.png',size:500},{name:'script.exe',size:5},{name:'empty.txt',size:0},{name:'huge.pdf',size:21*1024*1024}]);
  assert.equal(r.accepted.length,2);
  assert.equal(r.errors.length,3);
});

test('reset clears completed or revised runs',()=>{
  assert.deepEqual(runReducer({...initialRun,active:8,complete:true,revision:2},{type:'reset'}),initialRun);
});

test('pm returns to the customer, builders work together, and qa sends fixes back',()=>{
  let s={...initialRun};
  let pmHeld=false,customerReturned=false,buildOverlap=false,buildStagger=false,qaOverlap=false,qaStagger=false,pmWithLater=false;
  for(let i=0;i<2500&&!s.complete;i++){
    s=approveGates(runReducer(s,{type:'tick',amount:4}));
    const active=s.statuses.flatMap((status,index)=>status==='active'?[index]:[]);
    const done=s.statuses.flatMap((status,index)=>status==='done'?[index]:[]);
    if(s.statuses[3]==='hold'&&active.includes(0))pmHeld=true;
    if(pmHeld&&s.statuses[0]==='done'&&active.includes(3)&&s.statuses[4]==='pending')customerReturned=true;
    if(active.includes(5)&&active.includes(6))buildOverlap=true;
    if(buildOverlap&&done.includes(5)&&active.includes(6))buildStagger=true;
    if(active.includes(5)&&active.includes(6)&&active.includes(7))qaOverlap=true;
    if(s.statuses[3]==='active'&&[4,5,6,7,8,9].some(index=>active.includes(index)||s.statuses[index]==='awaiting'))pmWithLater=true;
    const group=[5,6,7];
    if(qaOverlap&&group.some(index=>active.includes(index))&&group.some(index=>done.includes(index)))qaStagger=true;
  }
  assert.equal(pmHeld,true);
  assert.equal(pmWithLater,true);
  assert.equal(customerReturned,true);
  assert.equal(buildOverlap,true);
  assert.equal(buildStagger,true);
  assert.equal(qaOverlap,true);
  assert.equal(qaStagger,true);
  assert.equal(s.complete,true);
  assert.equal(s.statuses.every(status=>status==='done'),true);
});

test('gates wait for approval and a uiux rejection returns to the customer',()=>{
  let s={...initialRun};
  for(let i=0;i<400&&s.statuses[1]!=='awaiting';i++)s=runReducer(s,{type:'tick',amount:4});
  assert.equal(s.statuses[1],'awaiting');
  assert.equal(s.statuses[2],'pending');
  let held=s;
  for(let i=0;i<20;i++)held=runReducer(held,{type:'tick',amount:4});
  assert.equal(held.statuses[1],'awaiting');
  const rejected=runReducer(s,{type:'reject',index:1,comment:'Tighten the acceptance criteria'});
  assert.equal(rejected.statuses[1],'active');
  assert.equal(rejected.progress,0);
  assert.equal(rejected.revisited[1],true);
  const approved=runReducer(s,{type:'approve',index:1});
  assert.equal(approved.statuses[1],'done');
  let design={...initialRun};
  for(let i=0;i<800&&design.statuses[4]!=='awaiting';i++)design=approveGates(runReducer(design,{type:'tick',amount:4}),[1,2]);
  let designHeld=design;
  for(let i=0;i<20;i++)designHeld=runReducer(designHeld,{type:'tick',amount:4});
  assert.equal(designHeld.statuses[4],'awaiting');
  let architect={...initialRun};
  for(let i=0;i<500&&architect.statuses[2]!=='awaiting';i++){
    architect=runReducer(architect,{type:'tick',amount:4});
    if(architect.statuses[1]==='awaiting')architect=runReducer(architect,{type:'approve',index:1});
  }
  assert.equal(architect.statuses[2],'awaiting');
  let architectHeld=architect;
  for(let i=0;i<20;i++)architectHeld=runReducer(architectHeld,{type:'tick',amount:4});
  assert.equal(architectHeld.statuses[2],'awaiting');
  assert.equal(design.statuses[4],'awaiting');
  const sentBack=runReducer(design,{type:'reject',index:4,comment:'Change the home screen'});
  assert.equal(sentBack.active,0);
  assert.equal(sentBack.statuses[0],'active');
  assert.equal(sentBack.statuses[4],'pending');
});
