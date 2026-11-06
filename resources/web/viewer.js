/* NavModel Manager offline viewer. THREE r160, MIT, bundled locally. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const canvas = $('canvas'), viewport = $('viewport');
  const error = text => {$('error').textContent = text; $('error').style.display = 'block';};
  window.addEventListener('error', e => error('Falha ao visualizar: ' + e.message));
  window.addEventListener('unhandledrejection', e => error('Falha ao carregar: ' + (e.reason?.message || e.reason)));
  const scene = new THREE.Scene(); scene.background = new THREE.Color('#293b4e');
  let renderer;
  try {renderer = new THREE.WebGLRenderer({canvas, antialias:true, powerPreference:'high-performance'});}
  catch(e){error('WebGL indisponível neste navegador. Habilite a aceleração gráfica ou use outro computador compatível.'); return;}
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.localClippingEnabled = true;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const origin = new THREE.Vector3(...MANIFEST.origin), bounds = MANIFEST.bounds;
  const extent = new THREE.Vector3(bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4]);
  const radius = Math.max(extent.length() * .5, .001);
  const saved = MANIFEST.camera;
  const camera = saved?.parallel ? new THREE.OrthographicCamera(-radius,radius,radius,-radius,.0001,radius*10000) : new THREE.PerspectiveCamera(45,1,.0001,radius*10000);
  camera.up.set(0,0,1);
  let dirty = false;
  let target = new THREE.Vector3(), distance = radius * 3, azimuth = -Math.PI/4, elevation = Math.PI/5;
  function setCamera(){camera.position.set(distance*Math.cos(elevation)*Math.cos(azimuth),distance*Math.cos(elevation)*Math.sin(azimuth),distance*Math.sin(elevation)).add(target);camera.lookAt(target);requestRender();}
  function syncOrbit(){const d=camera.position.clone().sub(target);distance=Math.max(d.length(),.00001);azimuth=Math.atan2(d.y,d.x);elevation=Math.asin(THREE.MathUtils.clamp(d.z/distance,-1,1));}
  if(saved){camera.position.fromArray(saved.position).sub(origin);target.fromArray(saved.target).sub(origin);camera.up.fromArray(saved.up);camera.lookAt(target);syncOrbit();if(camera.isOrthographicCamera){camera.top=saved.scale;camera.bottom=-saved.scale;}}
  else setCamera();
  scene.add(new THREE.AmbientLight(0xffffff,1.7));
  const key=new THREE.DirectionalLight(0xffffff,2.2);key.position.set(1,-2,3);scene.add(key);
  const fill=new THREE.DirectionalLight(0xb9d9ff,.6);fill.position.set(-2,1,1);scene.add(fill);
  const modelRoot=new THREE.Group();scene.add(modelRoot);
  const actors = new Map(), pickables=[], rows=[];
  let selected=null, measureMode=false, firstPoint=null, measurement=null, pointerDown=null, hoverTimer=null, lastPointer=null;
  function requestRender(){if(!dirty){dirty=true;requestAnimationFrame(()=>{dirty=false;renderer.render(scene,camera);});}}
  function resize(){const w=viewport.clientWidth,h=viewport.clientHeight;renderer.setSize(w,h,false);camera.aspect=w/h;if(camera.isOrthographicCamera){camera.left=-camera.top*w/h;camera.right=camera.top*w/h;}camera.updateProjectionMatrix();requestRender();}
  new ResizeObserver(resize).observe(viewport);resize();
  const fit=()=>{target.set(0,0,0);distance=radius*3;if(camera.isOrthographicCamera){camera.top=radius*1.2;camera.bottom=-camera.top;camera.zoom=1;resize();}setCamera();};
  $('fit').onclick=fit;
  $('iso').onclick=()=>{azimuth=-Math.PI/4;elevation=Math.PI/5;camera.up.set(0,0,1);setCamera();};
  $('top').onclick=()=>{elevation=Math.PI/2-.0001;azimuth=-Math.PI/2;camera.up.set(0,0,1);setCamera();};
  $('front').onclick=()=>{elevation=0;azimuth=-Math.PI/2;camera.up.set(0,0,1);setCamera();};
  canvas.addEventListener('contextmenu',e=>e.preventDefault());
  canvas.addEventListener('pointerdown',e=>{canvas.setPointerCapture(e.pointerId);pointerDown={x:e.clientX,y:e.clientY,lastX:e.clientX,lastY:e.clientY,button:e.button,moved:false};});
  canvas.addEventListener('pointermove',e=>{
    if(pointerDown){const dx=e.clientX-pointerDown.lastX,dy=e.clientY-pointerDown.lastY;pointerDown.lastX=e.clientX;pointerDown.lastY=e.clientY;if(Math.hypot(e.clientX-pointerDown.x,e.clientY-pointerDown.y)>3)pointerDown.moved=true;
      if(pointerDown.moved){$('snap').style.display='none';if(pointerDown.button===0&&!e.shiftKey){azimuth-=dx*.006;elevation=THREE.MathUtils.clamp(elevation+dy*.006,-Math.PI/2+.001,Math.PI/2-.001);camera.up.set(0,0,1);}else{const scale=camera.isOrthographicCamera?(camera.top-camera.bottom)/camera.zoom/viewport.clientHeight:2*distance*Math.tan(Math.PI/8)/viewport.clientHeight;const right=new THREE.Vector3().setFromMatrixColumn(camera.matrix,0),up=new THREE.Vector3().setFromMatrixColumn(camera.matrix,1);target.addScaledVector(right,-dx*scale).addScaledVector(up,dy*scale);}setCamera();}return;
    }
    if(measureMode){lastPointer={clientX:e.clientX,clientY:e.clientY};if(!hoverTimer)hoverTimer=setTimeout(()=>{hoverTimer=null;if(!lastPointer)return;const hit=pick(lastPointer);snap(hit);},80);}
  });
  canvas.addEventListener('pointerup',e=>{if(pointerDown&&!pointerDown.moved&&pointerDown.button===0){const hit=pick(e);if(hit){select(hit.object.userData.actorId,hit.instanceId);if(measureMode)measure(hit.point);}else if(measureMode)$('tip').textContent='Nenhuma face visível sob o cursor.';}pointerDown=null;});
  canvas.addEventListener('pointercancel',()=>{pointerDown=null;});
  canvas.addEventListener('wheel',e=>{e.preventDefault();const factor=Math.exp(THREE.MathUtils.clamp(e.deltaY,-500,500)*.001);if(camera.isOrthographicCamera){camera.zoom=THREE.MathUtils.clamp(camera.zoom/factor,.0001,1e6);camera.updateProjectionMatrix();requestRender();}else{distance=THREE.MathUtils.clamp(distance*factor,radius*1e-7,radius*10000);setCamera();}},{passive:false});
  const raycaster=new THREE.Raycaster();raycaster.params.Line.threshold=radius*.0008;
  const mouse=new THREE.Vector2();
  function pick(event){const r=canvas.getBoundingClientRect();mouse.set((event.clientX-r.left)/r.width*2-1,1-(event.clientY-r.top)/r.height*2);camera.updateMatrixWorld();raycaster.setFromCamera(mouse,camera);let best=null;const candidates=[];
    // Object bounds reject complete components before touching triangles.
    for(const mesh of pickables){if(!mesh.visible||!mesh.parent.visible)continue;const box=mesh.userData.worldBox;if(box&&raycaster.ray.intersectsBox(box))candidates.push(mesh);}
    const intersections=raycaster.intersectObjects(candidates,false);
    for(const hit of intersections){if(renderer.clippingPlanes.every(p=>p.distanceToPoint(hit.point)>=-1e-8)){best=hit;break;}}return best;
  }
  function snap(hit){if(!hit){$('snap').style.display='none';return;}const p=hit.point.clone().project(camera);$('snap').style.left=((p.x+1)*viewport.clientWidth/2)+'px';$('snap').style.top=((1-p.y)*viewport.clientHeight/2)+'px';$('snap').style.display='block';}
  function select(id,instanceId){if(selected){for(const mesh of selected.children){if(mesh.material?.emissive)mesh.material.emissive.setHex(0);}}selected=actors.get(id);if(!selected)return;for(const mesh of selected.children){if(mesh.material?.emissive)mesh.material.emissive.setHex(0x183448);}const a=MANIFEST.actors[id];const source=MANIFEST.models[a.model].name;const label=instanceId!==undefined?(a.names[instanceId]||a.name):a.name;const b=a.bounds;
    $('properties').textContent=`Nome: ${label}\nArquivo: ${source}\nCamada: ${a.layer}\nUnidades: m\nCor RGB: ${a.color.map(c=>Math.round(c*255)).join(', ')}\nPosição central: ${[0,1,2].map(i=>((b[i*2]+b[i*2+1])/2).toFixed(4)).join(' / ')}\nDimensões X/Y/Z: ${[0,1,2].map(i=>(b[i*2+1]-b[i*2]).toFixed(4)).join(' / ')}\n${Object.entries(a.properties).map(([k,v])=>k+': '+(Array.isArray(v)?v.join(', '):v)).join('\n')}`;
    for(const row of rows)row.element.classList.toggle('selected',row.id===id);requestRender();
  }
  function clearMeasurement(){firstPoint=null;if(measurement){scene.remove(measurement);measurement.geometry.dispose();measurement.material.dispose();measurement=null;}$('measure-result').textContent='Ative Medir e clique em dois pontos de uma face.';$('snap').style.display='none';requestRender();}
  $('clear').onclick=clearMeasurement;
  $('measure').onclick=()=>{measureMode=!measureMode;firstPoint=null;$('measure').classList.toggle('active',measureMode);$('tip').textContent=measureMode?'Medição: clique no ponto inicial e depois no ponto final. Esc cancela.':'Arrastar: orbitar • direito/meio: pan • roda: zoom';if(!measureMode)$('snap').style.display='none';};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){measureMode=false;firstPoint=null;$('measure').classList.remove('active');$('snap').style.display='none';}});
  function measure(p){snap({point:p});if(!firstPoint){firstPoint=p.clone();$('measure-result').textContent='Início (m): '+p.clone().add(origin).toArray().map(x=>x.toFixed(4)).join(' / ')+'\nSelecione o ponto final.';return;}const a=firstPoint.clone(),b=p.clone(),d=b.clone().sub(a);if(measurement){scene.remove(measurement);measurement.geometry.dispose();measurement.material.dispose();}measurement=new THREE.Line(new THREE.BufferGeometry().setFromPoints([a,b]),new THREE.LineBasicMaterial({color:0xffc22b,depthTest:false}));measurement.renderOrder=100;scene.add(measurement);$('measure-result').textContent=`Distância: ${d.length().toFixed(4)} m\nΔX: ${d.x.toFixed(4)} m\nΔY: ${d.y.toFixed(4)} m\nΔZ: ${d.z.toFixed(4)} m\nInício: ${a.add(origin).toArray().map(x=>x.toFixed(4)).join(' / ')}\nFim: ${b.add(origin).toArray().map(x=>x.toFixed(4)).join(' / ')}`;firstPoint=null;requestRender();}
  $('granularity').textContent=MANIFEST.granularity;
  const warningText=MANIFEST.models.flatMap(m=>(m.diagnostics||[]).map(w=>m.name+': '+w)).join('\n');if(warningText){$('warnings').className='warning';$('warnings').textContent='Avisos de importação da origem:\n'+warningText;}
  const modelTrees=MANIFEST.models.map(model=>{const d=document.createElement('details');d.open=true;const s=document.createElement('summary');s.textContent=model.name;d.appendChild(s);$('tree').appendChild(d);return {element:d,layers:new Map()};});
  for(const a of MANIFEST.actors){const mt=modelTrees[a.model];let layer=mt.layers.get(a.layer);if(!layer){layer=document.createElement('details');layer.open=true;const s=document.createElement('summary');s.textContent=a.layer;layer.appendChild(s);mt.element.appendChild(layer);mt.layers.set(a.layer,layer);}const row=document.createElement('div');row.className='tree-row';const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=a.visible;checkbox.setAttribute('aria-label','Visibilidade de '+a.name);checkbox.onchange=()=>{a.visible=checkbox.checked;const group=actors.get(a.id);if(group)group.visible=checkbox.checked;requestRender();};const button=document.createElement('button');button.textContent=a.name;button.title=a.name+(a.names.length>1?' • '+a.names.length+' componentes':'');button.onclick=()=>select(a.id);row.append(checkbox,button);layer.appendChild(row);rows.push({element:row,id:a.id,text:(a.name+' '+a.layer+' '+a.names.join(' ')).toLowerCase()});if(a.names.length>1){const members=document.createElement('details');const s=document.createElement('summary');s.textContent=a.names.length+' nomes de componentes';members.appendChild(s);members.addEventListener('toggle',()=>{if(members.open&&members.childElementCount===1){const list=document.createElement('div');list.className='muted';list.style.whiteSpace='pre-wrap';list.textContent=a.names.join('\n');members.appendChild(list);}});layer.appendChild(members);}}
  $('search').oninput=()=>{const q=$('search').value.toLowerCase();for(const row of rows)row.element.hidden=!row.text.includes(q);};
  function applyClips(){const mode=$('clip-mode').value;let equations=[];$('plane-controls').hidden=mode!=='plane';$('box-controls').hidden=mode!=='box';if(mode==='saved')equations=MANIFEST.clipping||[];
    if(mode==='plane'){const axis='XYZ'.indexOf($('clip-axis').value),sign=$('clip-invert').checked?-1:1,value=Number($('clip-offset').value);const eq=[0,0,0,-sign*value];eq[axis]=sign;equations.push(eq);}
    if(mode==='box'){for(let i=0;i<3;i++){const lo=Number($('box-'+i*2).value),hi=Number($('box-'+(i*2+1)).value);if(lo>hi){$('tip').textContent='Box: o mínimo precisa ser menor ou igual ao máximo.';return;}const lower=[0,0,0,-lo],upper=[0,0,0,hi];lower[i]=1;upper[i]=-1;equations.push(lower,upper);}}
    renderer.clippingPlanes=equations.map(eq=>new THREE.Plane(new THREE.Vector3(eq[0],eq[1],eq[2]),eq[3]+eq[0]*origin.x+eq[1]*origin.y+eq[2]*origin.z));for(const group of actors.values())for(const mesh of group.children)mesh.material.needsUpdate=true;requestRender();
  }
  for(let i=0;i<6;i++){$('box-'+i).value=bounds[i];$('box-'+i).oninput=applyClips;}
  function axisChanged(){const axis='XYZ'.indexOf($('clip-axis').value);$('clip-offset').value=(bounds[axis*2]+bounds[axis*2+1])/2;$('clip-slider').value=500;applyClips();}
  $('clip-mode').onchange=applyClips;$('clip-axis').onchange=axisChanged;$('clip-offset').oninput=applyClips;$('clip-invert').onchange=applyClips;$('clip-slider').oninput=()=>{const i='XYZ'.indexOf($('clip-axis').value);$('clip-offset').value=bounds[i*2]+(bounds[i*2+1]-bounds[i*2])*Number($('clip-slider').value)/1000;applyClips();};axisChanged();
  async function chunk(name){if(INLINE_CHUNKS){const base64=INLINE_CHUNKS[name];if(!base64)throw Error('Recurso ausente: '+name);const raw=atob(base64);const array=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)array[i]=raw.charCodeAt(i);delete INLINE_CHUNKS[name];return array.buffer;}const response=await fetch(name,{cache:'no-store'});if(!response.ok)throw Error('Falha no recurso '+name);return response.arrayBuffer();}
  function geometry(buffer){const h=new DataView(buffer);if(h.getUint32(0,true)!==0x314d564e)throw Error('Geometria inválida.');const points=h.getUint32(4,true),indices=h.getUint32(8,true),lines=h.getUint32(12,true);const exact=new Float64Array(buffer,16,points*3);const positions=new Float32Array(exact);let offset=16+points*24;const faces=new Uint32Array(buffer,offset,indices);offset+=indices*4;const segments=new Uint32Array(buffer,offset,lines);const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(positions,3));if(indices){g.setIndex(new THREE.BufferAttribute(faces,1));g.computeVertexNormals();}g.computeBoundingBox();g.computeBoundingSphere();return {g,segments,indices,positions};}
  function actorMatrix(a,localOrigin,instance){const m=new THREE.Matrix4().set(...a.matrix.flat());const t=new THREE.Matrix4().makeTranslation(localOrigin[0]+(instance?.[0]||0),localOrigin[1]+(instance?.[1]||0),localOrigin[2]+(instance?.[2]||0));m.multiply(t);m.elements[12]-=origin.x;m.elements[13]-=origin.y;m.elements[14]-=origin.z;return m;}
  async function load(){let loaded=0;const total=MANIFEST.actors.reduce((n,a)=>n+(a.parts?.length||1),0);for(const a of MANIFEST.actors){const group=new THREE.Group();group.visible=a.visible;modelRoot.add(group);actors.set(a.id,group);let instances=null;if(a.instances)instances=new Float64Array(await chunk(a.instances.chunk));const parts=a.parts||[{chunk:a.chunk,localOrigin:a.localOrigin}];for(const part of parts){const {g,segments,indices,positions}=geometry(await chunk(part.chunk));const color=new THREE.Color(...a.color);const opacity=a.opacity;const material=indices?new THREE.MeshPhongMaterial({color,side:THREE.DoubleSide,flatShading:true,shininess:24,opacity,transparent:opacity<1}):new THREE.PointsMaterial({color,size:3,sizeAttenuation:false,opacity,transparent:opacity<1});let mesh;
        if(indices&&instances){mesh=new THREE.InstancedMesh(g,material,a.instances.count);for(let i=0;i<a.instances.count;i++)mesh.setMatrixAt(i,actorMatrix(a,part.localOrigin,instances.subarray(i*3,i*3+3)));mesh.instanceMatrix.needsUpdate=true;mesh.computeBoundingBox();mesh.computeBoundingSphere();}
        else{mesh=indices?new THREE.Mesh(g,material):new THREE.Points(g,material);mesh.matrix.copy(actorMatrix(a,part.localOrigin));mesh.matrixAutoUpdate=false;}
        mesh.userData.actorId=a.id;group.add(mesh);mesh.updateMatrixWorld(true);mesh.userData.worldBox=new THREE.Box3().setFromObject(mesh);if(indices)pickables.push(mesh);
        if(segments.length){const lg=new THREE.BufferGeometry();lg.setAttribute('position',new THREE.BufferAttribute(positions,3));lg.setIndex(new THREE.BufferAttribute(segments,1));const line=new THREE.LineSegments(lg,new THREE.LineBasicMaterial({color,opacity,transparent:opacity<1}));line.matrix.copy(actorMatrix(a,part.localOrigin));line.matrixAutoUpdate=false;line.userData.actorId=a.id;group.add(line);}
        loaded++;$('bar').style.width=(100*loaded/total)+'%';$('load').firstChild.textContent=`${loaded}/${total} blocos • ${MANIFEST.analysis.triangles.toLocaleString('pt-BR')} triângulos`;requestRender();await new Promise(resolve=>setTimeout(resolve,0));}
    }INLINE_CHUNKS=null;$('tip').textContent='Pronto. Arrastar: orbitar • direito/meio: pan • roda: zoom';window.NAVMODEL_READY={actors:actors.size,triangles:MANIFEST.analysis.triangles,origin:MANIFEST.origin};requestRender();}
  if(!INLINE_CHUNKS){setInterval(()=>fetch('ping').catch(()=>{}),15000);fetch('ping').catch(()=>{});}
  load().catch(e=>error(e.message));
})();
