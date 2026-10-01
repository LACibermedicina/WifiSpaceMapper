/* Motor 3D do CSI Space Mapper -- WebGL puro, sem dependencias externas.
   Camera orbital + primeira pessoa, nuvem de pontos, malha (pontos/aramado/
   superficie), grade metrica, eixos, roteador, trajetoria, caixa do comodo,
   reguas dinamicas (largura / profundidade / altura / distancia) e vetores.
   Tambem faz a captura de cliques por raycasting para desenhar as medidas. */
(function (global) {
  "use strict";

  const VS = `
attribute vec3 aPos;
attribute vec4 aCol;
uniform mat4 uMVP;
uniform float uSize;
varying vec4 vCol;
void main(){
  vCol = aCol;
  gl_Position = uMVP * vec4(aPos, 1.0);
  gl_PointSize = uSize * (300.0 / max(0.6, gl_Position.w));
}`;

  const FS = `
precision mediump float;
varying vec4 vCol;
uniform float uAlpha;
void main(){
  vec2 d = gl_PointCoord - vec2(0.5);
  if (dot(d,d) > 0.25) discard;
  gl_FragColor = vec4(vCol.rgb, vCol.a * uAlpha);
}`;

  const VS_L = `
attribute vec3 aPos;
uniform mat4 uMVP;
void main(){ gl_Position = uMVP * vec4(aPos, 1.0); }`;

  const FS_L = `
precision mediump float;
uniform vec4 uColor;
void main(){ gl_FragColor = uColor; }`;

  const VS_T = `
attribute vec3 aPos;
attribute vec3 aNrm;
uniform mat4 uMVP;
uniform mat3 uN;
varying float vL;
void main(){
  vec3 n = normalize(uN * aNrm);
  vec3 l1 = normalize(vec3(0.45, 0.75, 0.5));
  vec3 l2 = normalize(vec3(-0.5, -0.2, 0.7));
  vL = 0.32 + 0.52 * max(dot(n, l1), 0.0) + 0.28 * max(dot(n, l2), 0.0);
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

  const FS_T = `
precision mediump float;
varying float vL;
uniform vec4 uColor;
uniform float uAlpha;
void main(){ gl_FragColor = vec4(uColor.rgb * vL, uColor.a * uAlpha); }`;

  // ---------------------------------------------------------------- matrizes
  const M = {
    ident: () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]),
    mul(a, b) {                       // a * b  (a aplica-se depois de b)
      const o = new Float32Array(16);
      for (let c = 0; c < 4; c++)
        for (let r = 0; r < 4; r++) {
          let s = 0;
          for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
          o[c * 4 + r] = s;
        }
      return o;
    },
    persp(fovy, asp, near, far) {
      const f = 1 / Math.tan(fovy / 2), nf = 1 / (near - far);
      return new Float32Array([
        f / asp, 0, 0, 0,
        0, f, 0, 0,
        0, 0, (far + near) * nf, -1,
        0, 0, 2 * far * near * nf, 0]);
    },
    ortho(l, r, b, t, n, f) {
      return new Float32Array([
        2/(r-l), 0, 0, 0,
        0, 2/(t-b), 0, 0,
        0, 0, -2/(f-n), 0,
        -(r+l)/(r-l), -(t+b)/(t-b), -(f+n)/(f-n), 1]);
    },
    look(eye, ctr, up) {
      let z = norm(sub(eye, ctr));
      let x = norm(cross(up, z));
      let y = cross(z, x);
      return new Float32Array([
        x[0], y[0], z[0], 0,
        x[1], y[1], z[1], 0,
        x[2], y[2], z[2], 0,
        -dot(x, eye), -dot(y, eye), -dot(z, eye), 1]);
    },
    normalMat(m) {                     // inversa-transposta 3x3
      const a = m;
      const o = new Float32Array(9);
      const d = a[0]*(a[5]*a[10]-a[6]*a[9]) - a[4]*(a[1]*a[10]-a[2]*a[9]) + a[8]*(a[1]*a[6]-a[2]*a[5]);
      const id = Math.abs(d) < 1e-12 ? 1 : 1/d;
      o[0]=(a[5]*a[10]-a[6]*a[9])*id; o[1]=(a[6]*a[8]-a[4]*a[10])*id; o[2]=(a[4]*a[9]-a[5]*a[8])*id;
      o[3]=(a[2]*a[9]-a[1]*a[10])*id; o[4]=(a[0]*a[10]-a[2]*a[8])*id; o[5]=(a[1]*a[8]-a[0]*a[9])*id;
      o[6]=(a[1]*a[6]-a[2]*a[5])*id; o[7]=(a[2]*a[4]-a[0]*a[6])*id; o[8]=(a[0]*a[5]-a[1]*a[4])*id;
      return o;
    }
  };
  const sub = (a,b)=>[a[0]-b[0],a[1]-b[1],a[2]-b[2]];
  const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
  const dot = (a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
  function norm(a){const n=Math.hypot(a[0],a[1],a[2])||1;return [a[0]/n,a[1]/n,a[2]/n];}

  // ---------------------------------------------------------------- colormap
  const STOPS = [
    [0.00, 0.03, 0.14, 0.88],
    [0.25, 0.00, 0.72, 0.96],
    [0.50, 0.10, 0.86, 0.36],
    [0.75, 0.99, 0.86, 0.10],
    [1.00, 0.96, 0.11, 0.16]];
  function distColor(t) {
    t = Math.max(0, Math.min(1, t));
    for (let i = 0; i < STOPS.length - 1; i++) {
      const a = STOPS[i], b = STOPS[i + 1];
      if (t <= b[0]) {
        const k = (t - a[0]) / (b[0] - a[0] || 1);
        return [a[1] + (b[1]-a[1])*k, a[2] + (b[2]-a[2])*k, a[3] + (b[3]-a[3])*k, 1];
      }
    }
    return [0.96, 0.11, 0.16, 1];
  }

  // ---------------------------------------------------------------- programa
  function compile(gl, vs, fs) {
    const p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        throw new Error(gl.getShaderInfoLog(s) || "shader");
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS))
      throw new Error(gl.getProgramInfoLog(p) || "link");
    return p;
  }

  class Painter {
    constructor(canvas, opts) {
      this.canvas = canvas;
      const gl = this.gl = canvas.getContext("webgl", {
        antialias: true, alpha: false, preserveDrawingBuffer: false,
        depth: true, powerPreference: "high-performance" });
      if (!gl) throw new Error("WebGL indisponivel neste navegador");
      this.progPt = compile(gl, VS, FS);
      this.progLn = compile(gl, VS_L, FS_L);
      this.progTr = compile(gl, VS_T, FS_T);

      this.buf = { points: gl.createBuffer(), lines: gl.createBuffer(),
                   meshV: gl.createBuffer(), meshI: gl.createBuffer(),
                   meshN: gl.createBuffer(), tri: gl.createBuffer(),
                   triI: gl.createBuffer(), triN: gl.createBuffer() };

      this.state = {
        cam: "orbita", target: [0, 0, 0], yaw: -0.72, pitch: 0.42, dist: 9.2,
        fov: 48 * Math.PI / 180, mode: "solid", ptSize: 5,
        show: { grid: true, axes: true, router: true, traj: true, room: true },
        showMeas: true, showVec: true, walk: null
      };
      this.layers = { points: null, grid: null, axes: null, router: null,
                      traj: null, room: null, meas: null, vec: null };
      this.mesh = null;
      this.fps = 0;
      this._t = performance.now();
      this._drag = null;
      this._keys = {};
      this._loop = this._loop.bind(this);
      this._bindInput(opts || {});
      this._resize();
      global.addEventListener("resize", () => this._resize());
      requestAnimationFrame(this._loop);
    }

    // ------------------------------------------------------------ geometria
    _resize() {
      const dpr = Math.min(global.devicePixelRatio || 1, 2);
      const r = this.canvas.getBoundingClientRect();
      const w = Math.max(2, Math.floor(r.width * dpr));
      const h = Math.max(2, Math.floor(r.height * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w; this.canvas.height = h;
      }
      this.gl.viewport(0, 0, w, h);
      this.aspect = Math.max(0.2, r.width / Math.max(1, r.height));
    }

    upload(name, data, kind) {
      const gl = this.gl, b = this.buf[name];
      gl.bindBuffer(kind, b);
      gl.bufferData(kind, data, gl.DYNAMIC_DRAW);
      return b;
    }

    setPoints(flat) {                       // Float32Array [x,y,z,r,g,b,a] * n
      this.layers.points = { data: flat || new Float32Array(0), n: (flat ? flat.length / 7 : 0) };
    }

    setMesh(mesh) {
      if (!mesh || !mesh.vertices || !mesh.vertices.length || !mesh.triangles || !mesh.triangles.length) {
        this.mesh = null; return;
      }
      const v = mesh.vertices, f = mesh.triangles;
      const vf = new Float32Array(v.length * 3);
      const vc = new Float32Array(v.length * 4);
      let rmax = 1e-6;
      for (let i = 0; i < v.length; i++) rmax = Math.max(rmax, Math.hypot(v[i][0], v[i][1], v[i][2]));
      for (let i = 0; i < v.length; i++) {
        vf[i*3] = v[i][0]; vf[i*3+1] = v[i][1]; vf[i*3+2] = v[i][2];
        const c = distColor(Math.hypot(v[i][0], v[i][1], v[i][2]) / rmax);
        vc[i*4] = c[0]; vc[i*4+1] = c[1]; vc[i*4+2] = c[2]; vc[i*4+3] = 1;
      }
      const idx = new Uint32Array(f.length * 3);
      for (let i = 0; i < f.length; i++) {
        idx[i*3] = f[i][0]; idx[i*3+1] = f[i][1]; idx[i*3+2] = f[i][2];
      }
      // normais por vertice (para o sombreamento da superficie)
      const nrm = new Float32Array(v.length * 3);
      for (let i = 0; i < f.length; i++) {
        const a = v[f[i][0]], b = v[f[i][1]], c = v[f[i][2]];
        const u = sub(b, a), w = sub(c, a);
        const n = cross(u, w);
        for (const j of [f[i][0], f[i][1], f[i][2]]) {
          nrm[j*3] += n[0]; nrm[j*3+1] += n[1]; nrm[j*3+2] += n[2];
        }
      }
      for (let i = 0; i < v.length; i++) {
        const n = norm([nrm[i*3], nrm[i*3+1], nrm[i*3+2]]);
        nrm[i*3] = n[0]; nrm[i*3+1] = n[1]; nrm[i*3+2] = n[2];
      }
      // arestas unicas do aramado
      const seen = new Set(), edges = [];
      for (let i = 0; i < f.length; i++)
        for (const [a, b] of [[f[i][0], f[i][1]], [f[i][1], f[i][2]], [f[i][2], f[i][0]]]) {
          const k = a < b ? a + "|" + b : b + "|" + a;
          if (!seen.has(k)) { seen.add(k); edges.push(a, b); }
        }
      const ef = new Float32Array(edges.length * 3);
      for (let i = 0; i < edges.length; i++) {
        const p = v[edges[i]];
        ef[i*3] = p[0]; ef[i*3+1] = p[1]; ef[i*3+2] = p[2];
      }
      const rf = new Float32Array(v.length * 4);
      for (let i = 0; i < v.length; i++) {
        rf[i*4] = v[i][0]; rf[i*4+1] = v[i][1]; rf[i*4+2] = v[i][2];
        rf[i*4+3] = 0.85;
      }
      this.mesh = { vf, idx, nrm, vc, ef, rf, nIdx: idx.length, nEdge: edges.length,
                    nVert: v.length };
    }

    setLayer(name, flat, kind) { this.layers[name] = flat || null; }

    show(k, v) { this.state.show[k] = !!v; }
    setMode(m) { this.state.mode = m; }
    setPointSize(s) { this.state.ptSize = s; }
    setMeasMode(on) { this.state.measMode = !!on; this.canvas.classList.toggle("picking", !!on); }

    // -------------------------------------------------------------- cameras
    applyView(name, room) {
      const r = room || [4, 5, 2.8];
      const span = Math.max(r[0], r[1], r[2]);
      this.state.dist = span * 1.85;
      this.state.target = [0, 0, 0];
      if (name === "top") { this.state.pitch = 1.5533; this.state.yaw = -1.5708; }
      else if (name === "front") { this.state.pitch = 0.02; this.state.yaw = -1.5708; }
      else if (name === "side") { this.state.pitch = 0.02; this.state.yaw = 0; }
      else { this.state.pitch = 0.42; this.state.yaw = -0.72; }
      this.state.cam = "orbita"; this.state.walk = null;
    }
    reset(room) { this.applyView("iso", room); }

    setWalk(on) {
      if (on) {
        this.state.cam = "livre";
        this.state.walk = { pos: [this.state.target[0] + 1.9, this.state.target[1] + 1.6,
                                  this.state.target[2] + 0.6],
                            yaw: this.state.yaw, pitch: -0.12 };
      } else { this.state.cam = "orbita"; this.state.walk = null; }
    }

    _eyeTarget() {
      const s = this.state;
      if (s.cam === "livre" && s.walk) {
        const cp = Math.cos(s.walk.pitch);
        const fwd = [Math.cos(s.walk.yaw) * cp, Math.sin(s.walk.yaw) * cp, Math.sin(s.walk.pitch)];
        return [[s.walk.pos[0], s.walk.pos[1], s.walk.pos[2]],
                [s.walk.pos[0] + fwd[0], s.walk.pos[1] + fwd[1], s.walk.pos[2] + fwd[2]]];
      }
      const cp = Math.cos(s.pitch);
      const dir = [Math.cos(s.yaw) * cp, Math.sin(s.yaw) * cp, Math.sin(s.pitch)];
      return [[s.target[0] + dir[0]*s.dist, s.target[1] + dir[1]*s.dist, s.target[2] + dir[2]*s.dist],
              [s.target[0], s.target[1], s.target[2]]];
    }

    _viewProj() {
      const [eye, ctr] = this._eyeTarget();
      const v = M.look(eye, ctr, [0, 0, 1]);
      const span = Math.max(2.0, this.state.dist * 0.35);
      const p = this.state.cam === "livre"
        ? M.persp(this.state.fov, this.aspect, 0.05, 400)
        : M.persp(this.state.fov, this.aspect, Math.max(0.05, span * 0.01), 900);
      return { vp: M.mul(p, v), eye };
    }

    // ---------------------------------------------------------------- input
    _bindInput(opts) {
      const c = this.canvas, s = this.state;
      c.addEventListener("pointerdown", (e) => {
        c.setPointerCapture(e.pointerId);
        this._drag = { x: e.clientX, y: e.clientY, btn: e.button, moved: 0 };
      });
      c.addEventListener("pointermove", (e) => {
        if (!this._drag) return;
        const dx = e.clientX - this._drag.x, dy = e.clientY - this._drag.y;
        this._drag.x = e.clientX; this._drag.y = e.clientY;
        this._drag.moved += Math.abs(dx) + Math.abs(dy);
        if (s.cam === "livre" && s.walk) {
          s.walk.yaw -= dx * 0.005;
          s.walk.pitch = Math.max(-1.35, Math.min(1.35, s.walk.pitch - dy * 0.005));
        } else if (this._drag.btn === 0 && !opts.pickMode) {
          s.yaw -= dx * 0.006; s.pitch = Math.max(-1.5, Math.min(1.5, s.pitch + dy * 0.006));
        } else if (this._drag.btn === 2) {                 // pan
          const cp = Math.cos(s.pitch), k = s.dist * 0.0016;
          const right = [-Math.sin(s.yaw), Math.cos(s.yaw), 0];
          const up = [-Math.sin(s.pitch)*Math.cos(s.yaw), -Math.sin(s.pitch)*Math.sin(s.yaw), cp];
          for (let i = 0; i < 3; i++) {
            s.target[i] += (-dx * right[i] + dy * up[i]) * k;
            if (s.walk) s.walk.pos[i] += (-dx * right[i] + dy * up[i]) * k;
          }
        }
      });
      c.addEventListener("pointerup", (e) => {
        const moved = this._drag ? this._drag.moved : 0;
        const btn = this._drag ? this._drag.btn : e.button;
        this._drag = null;
        if (btn === 0 && moved < 5 && opts.onPick) opts.onPick(e);
      });
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        const f = Math.exp(e.deltaY * 0.0011);
        if (s.cam === "livre") this._walkMove([0, 0, 0], (1 - f) * 2.2, true);
        else s.dist = Math.max(0.6, Math.min(90, s.dist * f));
      }, { passive: false });
      global.addEventListener("keydown", (e) => {
        this._keys[e.key.toLowerCase()] = true;
        if (["w","a","s","d","q","e"].includes(e.key.toLowerCase()) && s.cam === "livre") e.preventDefault();
      });
      global.addEventListener("keyup", (e) => { this._keys[e.key.toLowerCase()] = false; });
    }

    _walkMove(right, fwd, vertical) {
      const w = this.state.walk; if (!w) return;
      const cp = Math.cos(w.pitch);
      const f = [Math.cos(w.yaw) * cp, Math.sin(w.yaw) * cp, vertical ? 0 : Math.sin(w.pitch)];
      const r = [-Math.sin(w.yaw), Math.cos(w.yaw), 0];
      w.pos[0] += (f[0]*fwd + r[0]*right) ;
      w.pos[1] += (f[1]*fwd + r[1]*right);
      w.pos[2] += (f[2]*fwd) + (vertical || 0);
    }

    _tickWalk(dt) {
      const w = this.state.walk, k = this._keys;
      if (!w) return;
      const sp = 2.4 * dt;
      let r = 0, f = 0;
      if (k.w) f += sp; if (k.s) f -= sp;
      if (k.a) r -= sp; if (k.d) r += sp;
      if (r || f) this._walkMove(r, f, 0);
      if (k.e) w.pos[2] += sp * 0.7;
      if (k.q) w.pos[2] -= sp * 0.7;
    }

    // ------------------------------------------------------------ picking
    unproject(clientX, clientY, planeZ) {
      const rect = this.canvas.getBoundingClientRect();
      const nx = ((clientX - rect.left) / rect.width) * 2 - 1;
      const ny = 1 - ((clientY - rect.top) / rect.height) * 2;
      const { vp, eye } = this._viewProj();
      const inv = invert(vp);
      if (!inv) return null;
      const p0 = xform(inv, [nx, ny, -1]);
      const p1 = xform(inv, [nx, ny, 1]);
      const dir = norm(sub(p1, p0));
      if (planeZ === undefined || planeZ === null) return { origin: eye, dir };
      const t = (planeZ - eye[2]) / (dir[2] || 1e-6);
      if (t < 0) return { origin: eye, dir, point: null };
      return { origin: eye, dir,
               point: [eye[0] + dir[0]*t, eye[1] + dir[1]*t, planeZ] };
    }

    pickPoint(clientX, clientY, cloud) {
      // 1) tenta a esfera de pontos em espaco de tela (rapido e previsivel)
      const rect = this.canvas.getBoundingClientRect();
      const { vp } = this._viewProj();
      let best = null, bestD = 22 * 22;
      const n = cloud ? cloud.length : 0;
      for (let i = 0; i < n; i += 4) {
        const w = proj(vp, [cloud[i], cloud[i+1], cloud[i+2]], rect);
        if (!w) continue;
        const dx = w[0] - clientX, dy = w[1] - clientY;
        const d = dx*dx + dy*dy;
        if (d < bestD) { bestD = d; best = [cloud[i], cloud[i+1], cloud[i+2]]; }
      }
      if (best) return best;
      // 2) sem sorte: plano horizontal na altura do centro da nuvem
      let z = 0;
      if (n) { let s = 0; for (let i = 2; i < n; i += 4) s += cloud[i]; z = s / (n/4); }
      const r = this.unproject(clientX, clientY, z);
      return r ? r.point : null;
    }
    pickOnFloor(clientX, clientY, floorZ) {
      const r = this.unproject(clientX, clientY, floorZ);
      return r ? r.point : null;
    }

    // ---------------------------------------------------------------- render
    draw(gl, prog, mode, n, color) {
      gl.useProgram(prog);
      const loc = gl.getAttribLocation(prog, "aPos");
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
      const uc = gl.getUniformLocation(prog, "uColor");
      if (uc) gl.uniform4fv(uc, color || [1,1,1,1]);
      gl.drawArrays(mode, 0, n);
    }

    _loop(now) {
      const dt = Math.min(0.05, (now - this._t) / 1000); this._t = now;
      this.fps = this.fps ? this.fps * 0.9 + (1/Math.max(dt,1e-3)) * 0.1 : 1/Math.max(dt,1e-3);
      const gl = this.gl, s = this.state;
      this._resize();
      this._tickWalk(dt);

      gl.clearColor(0.02, 0.035, 0.055, 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

      const { vp } = this._viewProj();

      // linhas auxiliares (grade, eixos, caixa, trajetoria)
      const lineProg = this.progLn;
      gl.useProgram(lineProg);
      lineProg.uMVP = gl.getUniformLocation(lineProg, "uMVP");
      lineProg.uColor = gl.getUniformLocation(lineProg, "uColor");
      gl.uniformMatrix4fv(lineProg.uMVP, false, vp);

      const drawLines = (buf, n, col, m) => {
        if (!n) return;
        gl.uniform4fv(lineProg.uColor, col);
        if (m) gl.uniformMatrix4fv(lineProg.uMVP, false, M.mul(vp, m));
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        this.draw(gl, lineProg, gl.LINES, n, col);
        if (m) gl.uniformMatrix4fv(lineProg.uMVP, false, vp);
      };
      if (s.show.grid && this.layers.grid) {
        gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", this.layers.grid, gl.ARRAY_BUFFER));
        gl.drawArrays(gl.LINES, 0, this.layers.grid.length / 3);
      }
      if (s.show.room && this.layers.room) {
        gl.uniform4fv(lineProg.uColor, [0.45, 0.7, 1, 0.5]);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", this.layers.room, gl.ARRAY_BUFFER));
        gl.drawArrays(gl.LINES, 0, this.layers.room.length / 3);
      }
      if (s.show.axes && this.layers.axes) {
        const a = this.layers.axes;
        for (const [off, cnt, col] of [[0, 2, [0.95,0.2,0.2,1]], [2, 2, [0.25,0.9,0.3,1]],
                                        [4, 2, [0.3,0.55,1,1]]]) {
          gl.uniform4fv(lineProg.uColor, col);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", a.slice(off*3, (off+cnt)*3), gl.ARRAY_BUFFER));
          gl.drawArrays(gl.LINES, 0, 2);
        }
      }
      if (s.show.traj && this.layers.traj) {
        gl.uniform4fv(lineProg.uColor, [1, 0.8, 0.2, 0.75]);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", this.layers.traj, gl.ARRAY_BUFFER));
        gl.drawArrays(gl.LINE_STRIP, 0, this.layers.traj.length / 3);
      }

      // malha (3 modos)
      if (this.mesh) {
        const m = this.mesh;
        if (s.mode === "solid") {
          const tp = this.progTr;
          gl.useProgram(tp);
          const uMVP = gl.getUniformLocation(tp, "uMVP");
          const uN = gl.getUniformLocation(tp, "uN");
          gl.uniformMatrix4fv(uMVP, false, vp);
          gl.uniformMatrix3fv(uN, false, M.normalMat(vp));
          gl.uniform4fv(gl.getUniformLocation(tp, "uColor"), [1, 1, 1, 1]);
          gl.uniform1f(gl.getUniformLocation(tp, "uAlpha"), 0.97);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("tri", m.vf, gl.ARRAY_BUFFER));
          const lp = gl.getAttribLocation(tp, "aPos");
          gl.enableVertexAttribArray(lp); gl.vertexAttribPointer(lp, 3, gl.FLOAT, false, 0, 0);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("triN", m.nrm, gl.ARRAY_BUFFER));
          const ln = gl.getAttribLocation(tp, "aNrm");
          gl.enableVertexAttribArray(ln); gl.vertexAttribPointer(ln, 3, gl.FLOAT, false, 0, 0);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, this.upload("triI", m.idx, gl.ELEMENT_ARRAY_BUFFER));
          gl.enable(gl.POLYGON_OFFSET_FILL);
          gl.polygonOffset(1.0, 1.0);
          gl.drawElements(gl.TRIANGLES, m.nIdx, gl.UNSIGNED_INT, 0);
          gl.disable(gl.POLYGON_OFFSET_FILL);
        } else if (s.mode === "wire") {
          gl.uniform4fv(lineProg.uColor, [0.35, 0.85, 1, 0.32]);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("meshV", m.ef, gl.ARRAY_BUFFER));
          gl.drawArrays(gl.LINES, 0, m.nEdge);
        }
      }

      // nuvem de pontos
      if (this.layers.points && this.layers.points.n) {
        const pp = this.progPt;
        gl.useProgram(pp);
        const uMVP = gl.getUniformLocation(pp, "uMVP");
        gl.uniformMatrix4fv(uMVP, false, vp);
        gl.uniform1f(gl.getUniformLocation(pp, "uSize"), s.ptSize);
        gl.uniform1f(gl.getUniformLocation(pp, "uAlpha"), 1.0);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("points", this.layers.points.data, gl.ARRAY_BUFFER));
        const la = gl.getAttribLocation(pp, "aPos");
        gl.enableVertexAttribArray(la);
        gl.vertexAttribPointer(la, 3, gl.FLOAT, false, 28, 0);
        const lc = gl.getAttribLocation(pp, "aCol");
        gl.enableVertexAttribArray(lc);
        gl.vertexAttribPointer(lc, 4, gl.FLOAT, false, 28, 12);
        gl.drawArrays(gl.POINTS, 0, this.layers.points.n);
      }

      // medidas + vetores (linhas coloridas)
      gl.useProgram(lineProg);
      gl.uniformMatrix4fv(lineProg.uMVP, false, vp);
      if (s.showMeas && this.layers.meas) {
        const a = this.layers.meas;
        for (const seg of a) {
          gl.uniform4fv(lineProg.uColor, seg.color);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", new Float32Array(seg.pts), gl.ARRAY_BUFFER));
          gl.drawArrays(gl.LINES, 0, 2);
        }
      }
      if (s.showVec && this.layers.vec) {
        for (const seg of this.layers.vec) {
          gl.uniform4fv(lineProg.uColor, seg.color);
          gl.bindBuffer(gl.ARRAY_BUFFER, this.upload("lines", new Float32Array(seg.pts), gl.ARRAY_BUFFER));
          gl.drawArrays(seg.closed ? gl.LINE_LOOP : gl.LINE_STRIP, 0, seg.count);
        }
      }
      // tracinho em curso do vetor
      if (this.layers.vecLive && this.layers.vecLive.pts.length >= 6) {
        gl.uniform4fv(lineProg.uColor, this.layers.vecLive.color);
        gl.bindBuffer(gl.ARRAY_BUFFER,
          this.upload("lines", new Float32Array(this.layers.vecLive.pts), gl.ARRAY_BUFFER));
        gl.drawArrays(gl.LINE_STRIP, 0, this.layers.vecLive.pts.length / 3);
      }

      requestAnimationFrame(this._loop);
    }
  }

  // ------------------------------------------------------------- utilitarios
  function invert(m) {
    const o = new Float32Array(16);
    const a00=m[0],a01=m[1],a02=m[2],a03=m[3], a10=m[4],a11=m[5],a12=m[6],a13=m[7],
          a20=m[8],a21=m[9],a22=m[10],a23=m[11], a30=m[12],a31=m[13],a32=m[14],a33=m[15];
    const b00=a00*a11-a01*a10, b01=a00*a12-a02*a10, b02=a00*a13-a03*a10,
          b03=a01*a12-a02*a11, b04=a01*a13-a03*a11, b05=a02*a13-a03*a12,
          b06=a20*a31-a21*a30, b07=a20*a32-a22*a30, b08=a20*a33-a23*a30,
          b09=a21*a32-a22*a31, b10=a21*a33-a23*a31, b11=a22*a33-a23*a32;
    let det = b00*b11-b01*b10+b02*b09+b03*b08-b04*b07+b05*b06;
    if (!det) return null;
    det = 1/det;
    o[0]=(a11*b11-a12*b10+a13*b09)*det; o[1]=(a02*b10-a01*b11-a03*b09)*det;
    o[2]=(a31*b05-a32*b04+a33*b03)*det; o[3]=(a22*b04-a21*b05-a23*b03)*det;
    o[4]=(a12*b08-a10*b11-a13*b07)*det; o[5]=(a00*b11-a02*b08+a03*b07)*det;
    o[6]=(a32*b02-a30*b05-a33*b01)*det; o[7]=(a20*b05-a22*b02+a23*b01)*det;
    o[8]=(a10*b10-a11*b08+a13*b06)*det; o[9]=(a01*b08-a00*b10-a03*b06)*det;
    o[10]=(a30*b04-a31*b02+a33*b00)*det; o[11]=(a21*b02-a20*b04-a23*b00)*det;
    o[12]=(a11*b07-a10*b09-a12*b06)*det; o[13]=(a00*b09-a01*b07+a02*b06)*det;
    o[14]=(a31*b01-a30*b03-a32*b00)*det; o[15]=(a20*b03-a21*b01+a22*b00)*det;
    return o;
  }
  function xform(m, v) {
    const x = m[0]*v[0]+m[4]*v[1]+m[8]*v[2]+m[12];
    const y = m[1]*v[0]+m[5]*v[1]+m[9]*v[2]+m[13];
    const z = m[2]*v[0]+m[6]*v[1]+m[10]*v[2]+m[14];
    const w = m[3]*v[0]+m[7]*v[1]+m[11]*v[2]+m[15];
    return [x, y, z, w];
  }
  function proj(vp, p, rect) {
    const c = xform(vp, p);
    if (c[3] <= 0.0001) return null;
    const nx = c[0]/c[3], ny = c[1]/c[3];
    if (nx < -1.4 || nx > 1.4 || ny < -1.4 || ny > 1.4) return null;
    return [rect.left + (nx*0.5+0.5)*rect.width, rect.top + (1-(ny*0.5+0.5))*rect.height];
  }

  // ------------------------------------------------- camada de conveniencia
  function buildAux(room) {
    const [sx, sy, sz] = room, x0=-sx/2, x1=sx/2, y0=-sy/2, y1=sy/2, z0=-1.4, z1=sz-1.4;
    const grid = [], box = [], axes = [];
    for (let x = Math.ceil(x0); x <= x1; x++) grid.push(x, y0, z0, x, y1, z0);
    for (let y = Math.ceil(y0); y <= y1; y++) grid.push(x0, y, z0, x1, y, z0);
    const c = [[x0,y0,z0],[x1,y0,z0],[x1,y1,z0],[x0,y1,z0],[x0,y0,z1],[x1,y0,z1],[x1,y1,z1],[x0,y1,z1]];
    const e = [[0,1],[1,2],[2,3],[3,0],[4,5],[5,6],[6,7],[7,4],[0,4],[1,5],[2,6],[3,7]];
    for (const [a,b] of e) box.push(...c[a], ...c[b]);
    const L = Math.max(sx, sy) * 0.55;
    axes.push(0,0,0, L,0,0,  0,0,0, 0,L,0,  0,0,0, 0,0,L);
    return { grid: new Float32Array(grid), room: new Float32Array(box), axes: new Float32Array(axes) };
  }

  global.CSIGL = { Painter, distColor, buildAux, M, invert, xform, proj };
})(window);
