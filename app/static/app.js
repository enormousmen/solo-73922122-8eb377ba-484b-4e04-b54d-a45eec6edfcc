const ZOOMS = [0, 1, 2, 3];
const KINDS = ["regular", "emergency"];
const CN = {
  idle: "空闲", queued: "排队中", running: "渲染中",
  interrupted: "被抢占·待续算", done: "已完成", failed: "失败",
};
const KIND_CN = { regular: "常规底图", emergency: "应急航片" };

const cells = {};

function buildGrid(kind) {
  const host = document.querySelector(`#panel-${kind} .pyramid`);
  const map = {};
  for (const z of ZOOMS) {
    const n = 2 ** z;
    const row = document.createElement("div");
    row.className = "zrow";
    row.style.gridTemplateColumns = `repeat(${n}, 1fr)`;
    row.style.maxWidth = `${Math.min(n, 8) * 64}px`;
    for (let y = 0; y < n; y++) {
      for (let x = 0; x < n; x++) {
        const a = document.createElement("a");
        a.className = "cell empty";
        a.target = "_blank";
        a.title = `${kind} z${z} ${x}/${y}`;
        row.appendChild(a);
        map[`${z}/${x}/${y}`] = a;
      }
    }
    host.appendChild(row);
  }
  return map;
}

for (const k of KINDS) cells[k] = buildGrid(k);

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const t = await r.text();
    alert(`操作被拒绝：${r.status} ${t}`);
  }
  poll();
}

document.querySelectorAll("button[data-act]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const act = btn.dataset.act;
    if (act === "import") {
      uploadMbtiles();
      return;
    }
    const kind = btn.closest(".panel").id.replace("panel-", "");
    if (act === "start") {
      let url = `/api/jobs/${kind}/start`;
      if (kind === "emergency" && document.getElementById("simulate-fail").checked) {
        url += "?simulate_fail=true";
      }
      post(url);
    } else if (act === "reset") {
      post(`/api/jobs/${kind}/reset`);
    }
  });
});

async function uploadMbtiles() {
  const input = document.getElementById("mbtiles-file");
  const info = document.getElementById("import-info");
  if (!input.files.length) {
    alert("请先选择 .mbtiles 文件（可先下载本地样包）");
    return;
  }
  const file = input.files[0];
  if (file.size > 8 * 1024 * 1024) {
    alert(`包 ${file.size} 字节，超过 8 MiB 上限`);
    return;
  }
  const fd = new FormData();
  fd.append("file", file);
  info.textContent = `上传中：${file.name}（${file.size} 字节）…`;
  const fail = document.getElementById("import-fail").checked ? "?import_fail_after=10" : "";
  let r;
  try {
    r = await fetch(`/api/import/mbtiles${fail}`, { method: "POST", body: fd });
  } catch (e) {
    info.textContent = "上传失败：网络错误";
    return;
  }
  if (!r.ok) {
    let t = "";
    try { t = await r.text(); } catch (e) { t = String(e); }
    info.textContent = `上传被拒绝：HTTP ${r.status} ${t}`;
    return;
  }
  poll();
}

const IMPORT_STATE = {
  verifying: "验包中", accepted: "已导入", rejected: "已拒收",
  partial: "导入中断（可重传）", void: "已作废（验包期间常规被重置）",
};

function renderImport(imp) {
  const info = document.getElementById("import-info");
  if (!imp || !imp.latest) {
    return;
  }
  const it = imp.latest;
  const state = it.state;
  let text = `【${IMPORT_STATE[state] || state}】${it.filename} · SHA-256 ${it.checksum.slice(0, 12)}…`;
  text += ` · ${it.message}`;
  if (state === "verifying") {
    text += ` · 已收 ${it.received} 张`;
  } else if (it.progress && (it.progress.imported || it.progress.skipped)) {
    text += ` · 补空位 ${it.progress.imported}，已存在跳过 ${it.progress.skipped}`;
  }
  if (it.duration_ms) text += ` · 用时 ${it.duration_ms} ms`;
  info.textContent = text;
  info.dataset.state = state;
}

async function poll() {
  let s;
  try {
    const r = await fetch("/api/status");
    s = await r.json();
  } catch (e) {
    return;
  }
  document.getElementById("global").textContent =
    "当前渲染进程：" + (s.running_kind ? KIND_CN[s.running_kind] : "无（空闲）");
  for (const kind of KINDS) {
    const j = s.jobs[kind];
    const panel = document.getElementById(`panel-${kind}`);
    panel.dataset.status = j.status;
    panel.querySelector(".badge").textContent = CN[j.status] || j.status;
    panel.querySelector(".fill").style.width = j.percent + "%";
    let info = `${j.published}/${j.total}（${j.percent}%）`;
    if (j.current_tile) info += ` · 正在渲染 ${j.current_tile.join("/")}`;
    if (j.plan) info += ` · 本轮计划 ${j.plan.remaining}/${j.plan.total} 张`;
    if (j.error) info += ` · 错误：${j.error}`;
    panel.querySelector(".info").textContent = info;
    const pub = new Set(j.tiles);
    for (const key in cells[kind]) {
      const a = cells[kind][key];
      const has = pub.has(key);
      if (has && !a.dataset.on) {
        a.dataset.on = "1";
        a.classList.remove("empty");
        const img = document.createElement("img");
        img.src = `/tiles/${kind}/${key}.png`;
        img.alt = key;
        a.href = img.src;
        a.appendChild(img);
      } else if (!has && a.dataset.on) {
        delete a.dataset.on;
        a.classList.add("empty");
        a.innerHTML = "";
        a.removeAttribute("href");
      }
    }
  }
  renderImport(s.import);
}

setInterval(poll, 500);
poll();
