import { readFileSync, writeFileSync } from 'fs';

const path = process.argv[2] ??
  'src/ThreeViewer.vue';

let src = readFileSync(path, 'utf8');

if (src.includes('currentPosDot')) {
  console.log('SKIP: already patched'); process.exit(0);
}

const patches = [
  // 1. 変数宣言
  [
    'let feedLineMap: Map<number, { start: number; end: number }> = new Map();',
    'let feedLineMap: Map<number, { start: number; end: number }> = new Map();\nlet currentPosDot: THREE.Group | null = null;'
  ],
  // 2. ensureCoreGroups でクロスヘア生成
  [
    '  workRotGroup.add(workAxes);\n\n  // ---- Backplot line',
    `  workRotGroup.add(workAxes);

  // ---- Current position crosshair ----
  {
    const _cg = new THREE.Group();
    const _r = 4 * _unitScale;
    const _sphere = new THREE.Mesh(
      new THREE.SphereGeometry(_r, 16, 16),
      new THREE.MeshBasicMaterial({ color: 0x00ff88, depthTest: false, depthWrite: false })
    );
    _sphere.renderOrder = 20;
    _cg.add(_sphere);
    const _cs = _r * 5;
    const _crossGeom = new THREE.BufferGeometry();
    _crossGeom.setAttribute('position', new THREE.Float32BufferAttribute([
      -_cs, 0, 0,  _cs, 0, 0,
       0, -_cs, 0,  0, _cs, 0,
       0, 0, -_cs,  0, 0, _cs,
    ], 3));
    const _cross = new THREE.LineSegments(_crossGeom,
      new THREE.LineBasicMaterial({ color: 0x00ff88, depthTest: false, depthWrite: false })
    );
    _cross.renderOrder = 20;
    _cg.add(_cross);
    workRotGroup.add(_cg);
    currentPosDot = _cg;
  }

  // ---- Backplot line`
  ],
  // 3. applyState でポジション更新
  [
    '    pushBackplotPoint([xl.x, xl.y, xl.z]);\n  }\n\n  // ---- Highlight current motion line',
    `    pushBackplotPoint([xl.x, xl.y, xl.z]);
  }

  // ---- Current position dot ----
  if (currentPosDot && st.work_pos) {
    const _wp = st.work_pos as unknown as number[];
    currentPosDot.position.set(_wp[0] ?? 0, _wp[1] ?? 0, _wp[2] ?? 0);
  }

  // ---- Highlight current motion line`
  ],
  // 4. tool レイヤーに currentPosDot を追加
  [
    '    case "tool":\n      if (toolMarker) toolMarker.visible = on;\n      break;',
    '    case "tool":\n      if (toolMarker) toolMarker.visible = on;\n      if (currentPosDot) currentPosDot.visible = on;\n      break;'
  ],
];

let ok = true;
for (const [old, next] of patches) {
  if (!src.includes(old)) {
    console.error('ERROR: marker not found:\n  ' + old.slice(0, 60));
    ok = false;
  } else {
    src = src.replace(old, next);
  }
}
if (!ok) process.exit(1);

writeFileSync(path, src, 'utf8');
console.log('OK: ThreeViewer.vue patched');
