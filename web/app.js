// Yedekleme paneli: index.html'in davranışı. İstekler API'nin kendi adresine gider.
// Ekranda görünen bütün yazılar dil dosyasından gelir (API: /lang -> api/lang/<dil>.json).
const DAYS = ["pzt", "sal", "car", "per", "cum", "cmt", "paz"];   // config'teki gün kodları (yazı değil)
const API_URL = "";   // arayüz API'nin içinden açılıyor; istekler aynı adrese gidiyor

let TEXTS = {};   // dil dosyasının yazıları: anahtar -> yazı

// Anahtarın yazısı; {ad} yerlerine params'taki değerler konur. Anahtar dosyada yoksa anahtarın kendisi görünür.
// Değer de bir kod + değer ise ({code, params}, örn. bir hata) önce o yazıya çevrilir.
function t(key, params = {}) {
  const text = key in TEXTS ? TEXTS[key] : key;
  if (typeof text !== "string") return text;   // gün / ay adları gibi listeler olduğu gibi döner
  return text.replace(/\{(\w+)\}/g, (m, name) => {
    if (!(name in params)) return m;
    const value = params[name];
    return value && typeof value === "object" ? errorText(value) : value;
  });
}
// HTML içine konacak yazı: yazı kaçışlanır, params'taki değerler (hazır HTML) olduğu gibi konur
function th(key, params = {}) {
  return esc(t(key)).replace(/\{(\w+)\}/g, (m, name) => (name in params ? params[name] : m));
}
const dayLabel = (i) => t("days.short")[i];   // 0 = pazartesi

// Servisten ya da API'den gelen hata: kod + değerler ({code, params}) ise dil dosyasından, düz yazıysa olduğu gibi.
// İşletim sistemi hatası (error.os): dil dosyasında errno'sunun karşılığı varsa o, yoksa hatanın kendi yazısı.
function errorText(error) {
  if (!error || typeof error !== "object") return error;
  const p = error.params || {};
  if (error.code === "error.os") {
    const key = "errno." + p.errno;
    if (!(key in TEXTS)) return p.text;
    return p.path ? t("error.os_path", { reason: t(key), path: p.path }) : t(key);
  }
  return t(error.code, p);
}
// İlk harfi büyük (dilin kuralıyla: Türkçede i -> İ)
const capitalize = (s) => (s ? s.charAt(0).toLocaleUpperCase(t("app.html_lang")) + s.slice(1) : s);

// index.html'deki sabit yazılar: data-t (yazı), data-t-placeholder, data-t-aria-label, data-t-title, data-t-tip
function applyTexts() {
  document.documentElement.lang = t("app.html_lang");
  document.title = t("app.title");
  document.querySelectorAll("[data-t]").forEach((el) => { el.textContent = t(el.dataset.t); });
  document.querySelectorAll("[data-t-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.tPlaceholder); });
  document.querySelectorAll("[data-t-aria-label]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.tAriaLabel)); });
  document.querySelectorAll("[data-t-title]").forEach((el) => { el.title = t(el.dataset.tTitle); });
  document.querySelectorAll("[data-t-tip]").forEach((el) => { el.dataset.tip = t(el.dataset.tTip); });
  $("pair-hint").innerHTML = th("machines.code_hint", { file: `<span class="mono">pairing_code.txt</span>` });
}

const $ = (id) => document.getElementById(id);
const form = $("job-form");
const addBtn = $("add-btn");

let jobs = [];
let machines = {};      // makine id'si -> makine adı (API'ye kayıtlı makineler)
let statuses = {};      // makine id'si -> makinenin API'ye son gönderdiği durum
let targetTypes = {};   // hedef türleri (API'deki targets.json): tür -> ad, alan yazısı, örnek yol
let editIndex = null;   // düzenlenen işin sırası; yeni iş eklenirken null

// İşleri API'den al
function load() {
  fetch(API_URL + "/jobs")
    .then((response) => {
      if (!response.ok) throw new Error(t("error.http", { status: response.status }));
      return response.json();
    })
    .then((data) => { jobs = data; render(); })
    .catch((error) => showApiError(t("jobs.load_failed", { error: error.message })));
}

// Kayıtlı makineleri API'den al; formdaki makine listesini yenile
function loadMachines() {
  return fetch(API_URL + "/machines")
    .then((response) => {
      if (!response.ok) throw new Error(t("error.http", { status: response.status }));
      return response.json();
    })
    .then((data) => {
      machines = {};
      data.forEach((m) => { machines[m.id] = m.name; });
      const select = $("machine");
      const current = select.value;
      select.innerHTML = `<option value="">${esc(t("common.pick_machine"))}</option>` +
        data.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("");
      select.value = current in machines ? current : "";
      render();
      renderMachines(data);
    })
    .catch((error) => showApiError(t("machines.load_failed", { error: error.message })));
}

// Ekranda makinenin id'si yerine adı görünür (adı bilinmiyorsa id)
function machineName(id) {
  return machines[id] || id;
}

// İşlerin son halini API'ye gönder
function save() {
  fetch(API_URL + "/jobs", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(jobs)
  })
    .then((response) => {
      if (!response.ok) throw new Error(t("error.http", { status: response.status }));
      $("api-error").hidden = true;
    })
    .catch((error) => {
      showApiError(t("jobs.save_failed", { error: error.message }));
      load();   // ekranda kaydedilmemiş bir iş kalmasın: listeyi API'deki gerçek haliyle yenile
    });
}

// Makinelerin durumunu API'den al (birkaç saniyede bir)
function loadStatus() {
  fetch(API_URL + "/status")
    .then((response) => {
      if (!response.ok) throw new Error(t("error.http", { status: response.status }));
      return response.json();
    })
    .then((data) => {
      statuses = {};
      data.forEach((row) => { statuses[row.machine_id] = row; });
      render();
      if (!$("overview-view").hidden) renderOverview();
    })
    .catch(() => {});   // durum alınamazsa liste eskisi gibi görünür; hata kutusu işler içindir
}

// Hedef türlerini API'den al (api/targets.json); sonra formdaki listeyi kur
function loadTargets() {
  apiJson("/targets")
    .then((data) => { targetTypes = data; if (form.hidden) fillTargetTypes(); render(); })
    .catch((error) => showApiError(t("targets.load_failed", { error: error.message })));
}
// Bir hedef türünün yazı olmayan bilgisi (local_path); targets.json'da yoksa boş
function targetInfo(type) {
  return targetTypes[type] || {};
}
// Hedef türünün yazısı (dil dosyasında target.<tür>.<ad>); yoksa boş
function targetText(type, name) {
  const key = "target." + type + "." + name;
  return key in TEXTS ? t(key) : "";
}
// Hedef türünün görünen adı; dil dosyasında yoksa türün kendi adı görünür
function typeLabel(type) {
  return targetText(type, "label") || type;
}


function timeText(iso) {
  return iso ? iso.replace("T", " ") : "—";
}

// Bir işin durumunu (son sonuç, ilerleme) gösteren HTML
function statusHtml(j) {
  const row = statuses[j.machine_id];
  const s = row && row.status.jobs.find((x) => x.source === j.source && x.target === j.target);
  if (!s) return `<div class="job-status">${esc(t("status.none"))}</div>`;

  const problem = s.state === "waiting_access" || s.state === "waiting_retry";
  let html = `<div class="line"><span class="state ${s.state === "running" ? "running" : problem ? "problem" : ""}">${esc(t("state." + s.state))}</span>`;
  if (s.state === "running") html += `<span>${esc(t("status.progress", { done: s.total - s.pending, total: s.total }))}</span>`;
  html += `<span>${esc(t("status.last_run", { time: timeText(s.last_run) }))}</span>`;
  html += `<span>${esc(t("status.next_run", { time: nextRunText(j.schedule, s.last_run) }))}</span>`;
  if (s.last_result) {
    html += s.last_result.failed === 0
      ? `<span class="ok">${esc(t("status.result_ok", { files: s.last_result.files }))}</span>`
      : `<span class="bad">${esc(t("status.result_failed", { files: s.last_result.files, failed: s.last_result.failed }))}</span>`;
  }
  html += `</div>`;
  if (s.last_error) html += `<div class="bad">${esc(errorText(s.last_error))}</div>`;

  // Şu an kopyalanan / yüklenen dosyalar ve yüzdeleri
  row.status.active
    .filter((a) => a.job_source === j.source && a.job_target === j.target)
    .forEach((a) => {
      const pct = a.size ? Math.min(100, Math.floor(a.copied * 100 / a.size)) : 0;
      html += `<div class="file"><span class="name">${esc(a.source)}</span>
        <span class="bar"><i style="width:${pct}%"></i></span><span class="pct">${esc(t("status.percent", { pct }))}</span></div>`;
    });

  return `<div class="job-status">${html}</div>`;
}

// ---- Loglar sekmesi ----
function loadLogs() {
  const params = new URLSearchParams();
  if ($("log-client").value) params.set("machine_id", $("log-client").value);
  if ($("log-level").value) params.set("level", $("log-level").value);
  fetch(API_URL + "/logs?" + params)
    .then((response) => {
      if (!response.ok) throw new Error(t("error.http", { status: response.status }));
      return response.json();
    })
    .then((rows) => {
      $("log-error").hidden = true;
      $("logs").innerHTML = rows.length ? rows.map((r) => `
        <div class="log">
          <time>${esc(timeText(r.time))}</time>
          <span class="client">${esc(r.machine_name || r.machine_id)}</span>
          <span class="lvl ${esc(r.level)}">${esc(t("level." + r.level))}</span>
          <div class="msg">${esc(r.code ? t(r.code, r.params) : r.message)}${r.error ? `<details><summary>${esc(t("logs.error_detail"))}</summary><pre>${esc(r.error)}</pre></details>` : ""}</div>
        </div>`).join("")
        : `<div class="empty">${esc(t("logs.empty"))}</div>`;
    })
    .catch((error) => {
      $("log-error").textContent = t("logs.load_failed", { error: error.message });
      $("log-error").hidden = false;
    });
}

// Seçeneklerin değeri makine id'si, görünen yazısı makine adı; ada göre sıralı
function machineOptions(ids) {
  return ids.sort((a, b) => machineName(a).localeCompare(machineName(b)))
    .map((id) => `<option value="${esc(id)}">${esc(machineName(id))}</option>`).join("");
}

// Makine filtresindeki seçenekler: işlerde ve durumlarda geçen makineler
function updateLogClients() {
  const ids = [...new Set([...jobs.map((j) => j.machine_id), ...Object.keys(statuses)])];
  const select = $("log-client");
  const current = select.value;
  select.innerHTML = `<option value="">${esc(t("common.all"))}</option>` + machineOptions(ids);
  select.value = ids.includes(current) ? current : "";
}

function showTab(name) {
  ["overview", "jobs", "logs", "restore", "machines", "settings"].forEach((tab) => {
    $(tab + "-view").hidden = tab !== name;
    $("tab-" + tab).setAttribute("aria-selected", String(tab === name));
  });
  // Üst banttaki sayfa başlığı ve açıklaması, sekmeye göre
  $("page-title").textContent = t("page." + name + ".title");
  $("page-desc").textContent = t("page." + name + ".desc");
  addBtn.hidden = name !== "jobs";   // "Yeni iş" butonu sadece İşler sekmesinde
  $("ov-add-machine").hidden = $("ov-add-job").hidden = name !== "overview";
  if (name === "overview") { loadOverview(); renderOverview(); }
  if (name === "logs") { updateLogClients(); loadLogs(); }
  if (name === "restore") updateRestoreClients();
  if (name === "machines") loadMachines();
  if (name === "settings") loadAccount();
}

$("tab-overview").addEventListener("click", () => showTab("overview"));
$("tab-jobs").addEventListener("click", () => showTab("jobs"));
$("tab-logs").addEventListener("click", () => showTab("logs"));
$("tab-restore").addEventListener("click", () => showTab("restore"));
$("tab-machines").addEventListener("click", () => showTab("machines"));
$("tab-settings").addEventListener("click", () => showTab("settings"));

// ---- Ayarlar ----
$("password-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("pw-error").hidden = true;
  $("pw-ok").hidden = true;
  const current = $("pw-current").value;
  const next = $("pw-new").value;
  const fail = (text) => { $("pw-error").textContent = text; $("pw-error").hidden = false; };
  if (!current || !next) return fail(t("settings.pw_required"));
  if (next.length < 8) return fail(t("settings.pw_too_short"));
  if (next !== $("pw-new2").value) return fail(t("settings.pw_mismatch"));
  fetch(API_URL + "/account/password", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current, new: next })
  }).then((response) => response.json().then((data) => {
    if (response.ok) {
      $("password-form").reset();
      $("pw-ok").textContent = t("settings.pw_changed");
      $("pw-ok").hidden = false;
    } else if (data.detail === "wrong_current_password") {
      fail(t("settings.pw_wrong"));
    } else if (response.status === 429) {
      fail(t("common.too_many_attempts"));
    } else {
      fail(t("settings.pw_failed", { status: response.status }));
    }
  }));
});

function loadAccount() {
  $("email-error").hidden = true;
  $("email-ok").hidden = true;
  apiJson("/account")
    .then((account) => { $("email").value = account.email; })
    .catch((error) => { $("email-error").textContent = t("settings.load_failed", { error: error.message }); $("email-error").hidden = false; });
}

$("email-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("email-error").hidden = true;
  $("email-ok").hidden = true;
  const email = $("email").value.trim();
  if (email && !email.includes("@")) {
    $("email-error").textContent = t("settings.email_invalid");
    $("email-error").hidden = false;
    return;
  }
  machineRequest("PUT", "/account/email", { email }).then((status) => {
    if (status === 200) {
      $("email-ok").textContent = email ? t("settings.email_saved", { email }) : t("settings.email_cleared");
      $("email-ok").hidden = false;
    } else {
      $("email-error").textContent = t("settings.email_failed", { status });
      $("email-error").hidden = false;
    }
  });
});

// ---- Makineler ----
// Makine eklemek: makinedeki pairing_code.txt'deki kod + bir ad girilir. Aynı hesapta aynı ad olamaz.
// İstek sonucu HTTP koduyla döner: hata mesajı koda göre seçilir.
function machineRequest(method, url, body) {
  return fetch(API_URL + url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  }).then((response) => response.status);
}

// Makine isteklerinin HTTP koduna göre hata yazısı (dil dosyasında: machines.error_400 ...)
const MACHINE_ERRORS = [400, 409, 429];

function renderMachines(list) {
  $("machine-list").innerHTML = list.length
    ? list.map((m) => `
      <div class="mrow">
        <span class="name">${esc(m.name)}</span>
        <button class="edit" type="button" data-id="${esc(m.id)}">${esc(t("machines.rename"))}</button>
        <button class="trash" type="button" data-id="${esc(m.id)}" aria-label="${esc(t("machines.remove"))}" title="${esc(t("machines.remove"))}"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6"/></svg></button>
      </div>`).join("")
    : `<div class="mrow muted">${esc(t("machines.empty"))}</div>`;
}

$("pair-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("pair-error").hidden = true;
  $("pair-ok").hidden = true;
  const code = $("pair-code").value.trim();
  const name = $("pair-name").value.trim();
  if (!code || !name) {
    $("pair-error").textContent = t("machines.pair_required");
    $("pair-error").hidden = false;
    return;
  }
  machineRequest("POST", "/machines/pair", { code, name }).then((status) => {
    if (status === 200) {
      $("pair-form").reset();
      $("pair-ok").textContent = t("machines.paired", { name });
      $("pair-ok").hidden = false;
      loadMachines();
    } else {
      $("pair-error").textContent = MACHINE_ERRORS.includes(status) ? t("machines.error_" + status) : t("machines.pair_failed", { status });
      $("pair-error").hidden = false;
    }
  });
});

$("machine-list").addEventListener("click", (e) => {
  const editBtn = e.target.closest(".edit");
  const trashBtn = e.target.closest(".trash");
  if (!editBtn && !trashBtn) return;
  const id = (editBtn || trashBtn).dataset.id;
  const name = machines[id];
  $("machine-error").hidden = true;
  const fail = (status) => {
    $("machine-error").textContent = status === 409 ? t("machines.error_409") : t("machines.action_failed", { status });
    $("machine-error").hidden = false;
  };

  if (editBtn) {
    const newName = prompt(t("machines.rename_prompt"), name);
    if (newName === null || !newName.trim() || newName.trim() === name) return;
    machineRequest("PUT", "/machines/" + encodeURIComponent(id) + "/name", { name: newName.trim() })
      .then((status) => { if (status === 200) loadMachines(); else fail(status); });
    return;
  }

  if (!confirm(t("machines.remove_confirm", { name }))) return;
  machineRequest("DELETE", "/machines/" + encodeURIComponent(id)).then((status) => {
    if (status === 200) { loadMachines(); load(); } else fail(status);
  });
});
$("log-client").addEventListener("change", loadLogs);
$("log-level").addEventListener("change", loadLogs);
setInterval(() => { if (loggedIn && !$("logs-view").hidden) loadLogs(); }, 5000);   // sekme açıkken 5 saniyede bir yenile

// ---- Geri yükleme ----
// Makine seçilir; o makinenin her işi için hedefteki tarih klasörleri listelenir, klasör açılınca dosyaları görünür.
// Hedef türü ne olursa olsun liste ve dosyalar servisten gelir (servis 5 saniyede bir sorar).
let restoreRun = 0;      // başka makine seçilirse eski isteklerin cevapları yok sayılsın
let restoreJobs = [];    // seçili makinenin işleri (indirmede hangi işe ait olduğu buradan bulunur)
const FOLDER_RE = /^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/;   // servisin oluşturduğu tarih klasörü adı

function sizeText(bytes) {
  if (bytes < 1024) return t("size.b", { n: bytes });
  if (bytes < 1048576) return t("size.kb", { n: (bytes / 1024).toFixed(1) });
  if (bytes < 1073741824) return t("size.mb", { n: (bytes / 1048576).toFixed(1) });
  return t("size.gb", { n: (bytes / 1073741824).toFixed(2) });
}

function apiJson(url, options) {
  return fetch(API_URL + url, options).then((response) => {
    if (!response.ok) throw new Error(t("error.http", { status: response.status }));
    return response.json();
  });
}

function postJson(url, body) {
  return apiJson(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

// İstek "pending" olduğu sürece 2 saniyede bir sor; bitince sonucu döndür
function waitRequest(id, run) {
  return apiJson("/restore/requests/" + id).then((r) => {
    if (run !== restoreRun) throw new Error("cancelled");
    if (r.status !== "pending") return r;
    return new Promise((resolve) => setTimeout(resolve, 2000)).then(() => waitRequest(id, run));
  });
}

// Makine listesi: işi olan makineler
function updateRestoreClients() {
  const ids = [...new Set(jobs.map((j) => j.machine_id))];
  const select = $("restore-client");
  const current = select.value;
  select.innerHTML = `<option value="">${esc(t("common.pick_machine"))}</option>` + machineOptions(ids);
  select.value = ids.includes(current) ? current : "";
}

// Dosyaları tarih klasörlerine ayır: "2026-11-23_02-00-00/alt/a.txt" -> klasör "2026-11-23_02-00-00", ad "alt/a.txt"
function groupByFolder(files) {
  const groups = {};
  files.forEach((f) => {
    const slash = f.path.indexOf("/");
    const first = slash > 0 ? f.path.slice(0, slash) : "";
    const folder = FOLDER_RE.test(first) ? first : "";   // tarih klasörü dışındakiler: eski yedekler
    const name = folder ? f.path.slice(slash + 1) : f.path;
    (groups[folder] = groups[folder] || []).push({ ...f, name });
  });
  // En yeni klasör en üstte; klasörsüz eski yedekler en altta
  return Object.keys(groups).sort((a, b) => (a === "" ? 1 : b === "" ? -1 : b.localeCompare(a)))
    .map((folder) => ({ folder, files: groups[folder].sort((x, y) => x.name.localeCompare(y.name)) }));
}

function loadRestore() {
  const run = ++restoreRun;
  const client = $("restore-client").value;
  restoreJobs = jobs.filter((j) => j.machine_id === client);
  $("restore-foot").hidden = true;
  updateRestoreCount();
  $("restore-jobs").innerHTML = restoreJobs.map((j, n) => `
    <div class="box rjob" id="rjob-${n}">
      <div class="rjob-head mono"><span class="client">${esc(j.source)}</span> → ${esc(typeLabel(j.target_type))} · ${esc(j.target)}</div>
      <p class="hint msg">${esc(t("restore.waiting"))}</p>
    </div>`).join("");

  restoreJobs.forEach((j, n) => {
    const box = $("rjob-" + n);
    postJson("/restore/list", { machine_id: j.machine_id, source: j.source, target: j.target })
      .then((r) => waitRequest(r.id, run))
      .then((r) => {
        if (r.status === "error") throw new Error(errorText(r.error));
        const groups = groupByFolder(r.files);
        if (!groups.length) { box.querySelector(".msg").textContent = t("restore.empty"); return; }
        box.querySelector(".msg").remove();
        box.insertAdjacentHTML("beforeend", groups.map((g) => `
          <div class="rfolder">
            <button class="rf-toggle" type="button" aria-expanded="false">
              <span class="arrow">▸</span>
              <span class="fname">${esc(g.folder || t("restore.no_folder"))}</span>
              <span class="meta">${esc(t("restore.files", { n: g.files.length }))}</span>
            </button>
            <div class="rf-files" hidden>${g.files.map((f) => `
              <label class="rfile">
                <input type="checkbox" data-job="${n}" data-path="${esc(f.path)}">
                <span class="name">${esc(f.name)}</span>
                <span class="meta">${esc(sizeText(f.size))} · ${esc(timeText(f.modified))}</span>
                <span class="rstate"></span>
              </label>`).join("")}
            </div>
          </div>`).join(""));
        $("restore-foot").hidden = false;
      })
      .catch((error) => {
        if (error.message === "cancelled") return;
        box.querySelector(".msg").textContent = t("restore.list_failed", { error: error.message });
      });
  });
}

function updateRestoreCount() {
  const count = $("restore-jobs").querySelectorAll("input:checked").length;
  $("restore-count").textContent = t("restore.selected", { n: count });
}

function downloadOne(box, run) {
  const job = restoreJobs[Number(box.dataset.job)];
  const state = box.closest(".rfile").querySelector(".rstate");
  state.classList.remove("err");
  state.textContent = t("restore.preparing");
  postJson("/restore/file", { machine_id: job.machine_id, source: job.source, target: job.target, path: box.dataset.path })
    .then((r) => waitRequest(r.id, run))
    .then((r) => {
      if (r.status === "error") throw new Error(errorText(r.error));
      // Tarayıcının normal indirmesi: dosya İndirilenler klasörüne iner
      const a = document.createElement("a");
      a.href = API_URL + "/restore/download/" + r.id;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
      state.textContent = t("restore.downloaded");
    })
    .catch((error) => {
      if (error.message === "cancelled") return;
      state.classList.add("err");
      state.textContent = t("restore.failed", { error: error.message });
    });
}

$("restore-client").addEventListener("change", loadRestore);
$("restore-jobs").addEventListener("click", (e) => {
  const toggle = e.target.closest(".rf-toggle");
  if (!toggle) return;
  const open = toggle.getAttribute("aria-expanded") !== "true";
  toggle.setAttribute("aria-expanded", String(open));
  toggle.nextElementSibling.hidden = !open;   // klasörün dosyalarını aç / kapat
});
$("restore-jobs").addEventListener("change", updateRestoreCount);
$("restore-download").addEventListener("click", () => {
  const run = restoreRun;
  $("restore-jobs").querySelectorAll("input:checked").forEach((box) => downloadOne(box, run));
});

function showApiError(message) {
  $("api-error").textContent = message;
  $("api-error").hidden = false;
}

// Gün seçimi butonları (dil dosyası yüklendikten sonra kurulur: start)
function renderDays() {
  $("days").innerHTML = DAYS.map((d, i) =>
    `<label><input type="checkbox" id="day-${d}" value="${d}"><span>${esc(dayLabel(i))}</span></label>`
  ).join("");
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function scheduleText(s) {
  if (s.type === "daily") return t("schedule.daily_text", { time: s.time });
  if (s.type === "weekly") return t("schedule.weekly_text", { days: s.days.map((d) => dayLabel(DAYS.indexOf(d))).join(", "), time: s.time });
  return t("schedule.interval_text", { minutes: s.minutes });
}

// Hedef türü listesi: targets.json'daki türler, dosyadaki sırayla (ilki seçili gelir)
function fillTargetTypes(selected) {
  const keys = Object.keys(targetTypes);
  // Düzenlenen işin türü targets.json'da yoksa yine de görünsün
  if (selected && !keys.includes(selected)) keys.push(selected);
  $("target-type").innerHTML = keys.map((k) => `<option value="${esc(k)}">${esc(typeLabel(k))}</option>`).join("");
  if (selected) $("target-type").value = selected;
}

// Hedef alanının yazısı ve örnek yolu: seçili türün targets.json'daki bilgisinden
function updateTargetField() {
  const type = $("target-type").value;
  $("target-label").textContent = targetText(type, "target_label") || t("form.target");
  $("target").placeholder = targetText(type, "placeholder").replace("{machine}", machines[$("machine").value] || t("form.machine_name_example"));
}

// Mod düğmeleri: seçili değeri oku / bir değeri seç
function getSeg(name) {
  return form.querySelector(`input[name="${name}"]:checked`).value;
}
function setSeg(name, value) {
  form.querySelector(`input[name="${name}"][value="${value}"]`).checked = true;
}


function updateScheduleFields() {
  const type = $("type").value;
  $("time-field").hidden = type === "interval";
  $("minutes-field").hidden = type !== "interval";
  $("days-field").hidden = type !== "weekly";
  $("schedule-hint").textContent = t("schedule." + type + "_hint");
}

// Yolları karşılaştırmak için: Windows'ta büyük/küçük harf ve sondaki \ fark etmez
function normPath(p) {
  return p.trim().replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

function validate(job) {
  if (!job.machine_id) return t("form.error_machine");
  if (!job.source) return t("form.error_source");
  if (!job.target_type) return t("form.error_target_type");
  if (!job.target) return t("form.error_target");
  if (targetInfo(job.target_type).local_path) {
    // Bu kontroller sadece iki klasör aynı bilgisayardayken anlamlı
    const src = normPath(job.source), dst = normPath(job.target);
    if (src === dst) return t("form.error_same_folder");
    if ((dst + "\\").startsWith(src + "\\")) return t("form.error_target_inside");
  }
  if (jobs.some((j, i) => i !== editIndex && j.machine_id === job.machine_id && j.source === job.source && j.target_type === job.target_type && j.target === job.target)) return t("form.error_duplicate");

  const s = job.schedule;
  if (s.type !== "interval" && !/^([01]\d|2[0-3]):[0-5]\d$/.test(s.time || "")) return t("form.error_time");
  if (s.type === "weekly" && !s.days.length) return t("form.error_days");
  if (s.type === "interval" && !(Number.isInteger(s.minutes) && s.minutes > 0)) return t("form.error_minutes");
  return null;
}

function readForm() {
  const type = $("type").value;
  const schedule = { type };
  if (type === "interval") {
    schedule.minutes = Number($("minutes").value);
  } else {
    schedule.time = $("time").value;
  }
  if (type === "weekly") {
    schedule.days = DAYS.filter((d) => $("day-" + d).checked);
  }
  return {
    machine_id: $("machine").value,
    source: $("source").value.trim(),
    target_type: $("target-type").value,
    target: $("target").value.trim(),
    schedule,
    mode: getSeg("mode"),
    compression: getSeg("compression"),
    backup_type: getSeg("backup-type")
  };
}

function render() {
  $("count").textContent = jobs.length ? t("jobs.count", { n: jobs.length }) : "";
  $("list").innerHTML = jobs.length
    ? jobs.map((j, i) => `
      <div class="job">
        <div class="route mono"><span class="client">${esc(machineName(j.machine_id))}</span><span>${esc(j.source)}</span><span class="to"><span class="type">${esc([t("mode." + (j.mode || "copy")), (j.compression || "none") === "zip" ? t("compression.zip_short") : "",
          t("backup_type." + (j.backup_type || "full")), typeLabel(j.target_type)].filter(Boolean).join(" · "))}</span>${esc(j.target)}</span></div>
        <span class="chip">${esc(scheduleText(j.schedule))}</span>
        <button class="edit" type="button" data-i="${i}">${esc(t("jobs.edit"))}</button>
        <button class="del" type="button" data-i="${i}" aria-label="${esc(t("jobs.delete", { source: j.source }))}">×</button>
        ${statusHtml(j)}
      </div>`).join("")
    : `<div class="empty"><b>${esc(t("jobs.empty_title"))}</b><br>${esc(t("jobs.empty_hint"))}</div>`;
  $("config").textContent = JSON.stringify({ jobs }, null, 4);
}

// Formu bir işin değerleriyle doldur (düzenleme için)
function fillForm(job) {
  $("machine").value = job.machine_id;
  $("source").value = job.source;
  fillTargetTypes(job.target_type);
  $("target").value = job.target;
  setSeg("mode", job.mode || "copy");
  setSeg("compression", job.compression || "none");
  setSeg("backup-type", job.backup_type || "full");
  updateTargetField();
  $("type").value = job.schedule.type;
  if (job.schedule.time) $("time").value = job.schedule.time;
  if (job.schedule.minutes) $("minutes").value = job.schedule.minutes;
  DAYS.forEach((d) => { $("day-" + d).checked = (job.schedule.days || []).includes(d); });
  updateScheduleFields();
}

function openForm(open, index = null) {
  editIndex = open ? index : null;
  form.reset();
  if (open) loadMachines();   // form açılırken yeni kaydolan makineler de listede olsun
  updateScheduleFields();
  fillTargetTypes();
  updateTargetField();
  if (editIndex !== null) fillForm(jobs[editIndex]);
  $("form-title").textContent = editIndex !== null ? t("form.title_edit") : t("form.title_new");
  $("submit-btn").textContent = editIndex !== null ? t("common.save") : t("form.add");
  form.hidden = !open;
  addBtn.setAttribute("aria-expanded", String(open));
  addBtn.textContent = open ? t("jobs.close_form") : t("jobs.new");
  $("form-error").hidden = true;
  if (open) $("machine").focus();
}

addBtn.addEventListener("click", () => openForm(form.hidden || editIndex !== null));
$("cancel-btn").addEventListener("click", () => openForm(false));
$("type").addEventListener("change", updateScheduleFields);
$("target-type").addEventListener("change", updateTargetField);
$("machine").addEventListener("change", updateTargetField);   // bulut türlerinde örnek yolda makinenin adı var

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const job = readForm();
  const error = validate(job);
  if (error) {
    $("form-error").textContent = error;
    $("form-error").hidden = false;
    return;
  }
  if (editIndex !== null) {
    jobs[editIndex] = job;   // düzenlenen işin yerine yeni halini koy
  } else {
    jobs.push(job);
  }
  save();
  render();
  openForm(false);
});

$("list").addEventListener("click", (e) => {
  const editBtn = e.target.closest(".edit");
  if (editBtn) {
    openForm(true, Number(editBtn.dataset.i));
    return;
  }
  const btn = e.target.closest(".del");
  if (!btn) return;
  jobs.splice(Number(btn.dataset.i), 1);
  if (editIndex !== null) openForm(false);   // silme sonrası sıralar kayar, açık düzenlemeyi kapat
  save();
  render();
});

$("copy-btn").addEventListener("click", () => {
  const text = $("config").textContent;
  const done = () => { $("copied").hidden = false; setTimeout(() => { $("copied").hidden = true; }, 1600); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, selectConfig);
  } else {
    selectConfig();
  }
});

function selectConfig() {
  const range = document.createRange();
  range.selectNodeContents($("config"));
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

// ---- Giriş ----
let loggedIn = false;

// API "giriş gerekli" (401) derse giriş ekranına dön (örn. API yeniden başladıysa oturum düşer)
const apiFetch = window.fetch.bind(window);
window.fetch = (url, options) => apiFetch(url, options).then((response) => {
  if (response.status === 401 && !String(url).endsWith("/login")) showLogin();
  return response;
});

function showLogin() {
  loggedIn = false;
  $("app").hidden = true;
  $("login-view").hidden = false;
  $("login-user").focus();
}

function showApp() {
  loggedIn = true;
  $("login-view").hidden = true;
  $("app").hidden = false;
  apiJson("/account").then((account) => { $("user-name").textContent = account.username; }).catch(() => {});
  loadMachines();
  loadTargets();
  load();
  loadStatus();
  showTab("overview");
}

$("login-view").addEventListener("submit", (e) => {
  e.preventDefault();
  fetch(API_URL + "/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("login-user").value, password: $("login-pass").value })
  })
    .then((response) => {
      $("login-pass").value = "";
      if (response.ok) {
        $("login-error").hidden = true;
        showApp();
        return;
      }
      $("login-error").textContent = response.status === 401
        ? t("login.wrong")
        : response.status === 429
        ? t("common.too_many_attempts")
        : t("login.failed", { status: response.status });
      $("login-error").hidden = false;
    })
    .catch((error) => {
      $("login-error").textContent = t("login.unreachable", { error: error.message });
      $("login-error").hidden = false;
    });
});

$("logout-btn").addEventListener("click", () => {
  fetch(API_URL + "/logout", { method: "POST" }).finally(() => location.reload());
});

setInterval(() => { if (loggedIn) loadStatus(); }, 3000);   // durum 3 saniyede bir yenilenir

// Açılışta: önce dil dosyası alınır, yazılar yerleştirilir; sonra giriş yapılmışsa panel, yapılmamışsa giriş ekranı
function start() {
  applyTexts();
  renderDays();
  updateScheduleFields();
  render();
  fetch(API_URL + "/me")
    .then((response) => response.json())
    .then((me) => (me.logged_in ? showApp() : showLogin()))
    .catch(() => showLogin());
}
fetch(API_URL + "/lang")
  .then((response) => response.json())
  .then((data) => { TEXTS = data.texts; })
  .catch(() => {})   // dil dosyası alınamazsa ekranda anahtarlar görünür
  .finally(start);

// ---- Genel bakış ----
// Veriler: makineler (machines), durumlar (statuses, 3 sn'de bir), işler (jobs) ve /overview
// (açık sorunlar, son 14 günün çalışmaları, seçilen günün olayları). Saatler API'nin saatine göre hesaplanır.
let overview = null;
let clockOffset = 0;     // API saati - tarayıcı saati (ms)
let ovDay = null;        // seçilen gün "YYYY-AA-GG"; null = bugün
let ovMode = "day";      // "day": günün zaman çizelgesi, "14": son 14 gün
const ONLINE_SECONDS = 120;   // son haber bundan yeniyse makine çevrimiçi sayılır (servis 5 sn'de bir haber verir)
const pad = (n) => String(n).padStart(2, "0");
const dayKey = (d) => d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
const apiNow = () => new Date(Date.now() + clockOffset);
const jobKey = (j) => j.source + " -> " + j.target;
const monthName = (i) => t("months")[i];      // 0 = ocak
const weekdayName = (i) => t("weekdays")[i];  // 0 = pazar (Date.getDay sırası)

function loadOverview() {
  apiJson("/overview" + (ovDay ? "?day=" + ovDay : ""))
    .then((data) => {
      overview = data;
      clockOffset = new Date(data.now) - Date.now();
      $("ov-error").hidden = true;
      renderOverview();
    })
    .catch((error) => {
      $("ov-error").textContent = t("overview.load_failed", { error: error.message });
      $("ov-error").hidden = false;
    });
}
setInterval(() => { if (loggedIn && !$("overview-view").hidden) loadOverview(); }, 10000);

// Sayıdan sonra gelen iyelik eki ("3'ü", "5'i", "2'si"). Ekler dil dosyasındaki "number.suffix" bilgisinden;
// dilde böyle bir ek yoksa (bilgi yoksa) sayı olduğu gibi döner.
function possessive(n) {
  const sx = TEXTS["number.suffix"];
  if (!sx) return String(n);
  let suffix;
  if (n === 0) suffix = sx.zero;
  else if (n % 10) suffix = sx.units[n % 10];
  else if (n % 100) suffix = sx.tens[(n % 100) / 10];
  else suffix = n % 1000 ? sx.hundred : sx.thousand;
  return t("number.suffix_format", { n, suffix });
}

function agoText(date) {
  const s = Math.max(0, Math.round((apiNow() - date) / 1000));
  if (s < 60) return t("time.ago_sec", { n: s });
  if (s < 3600) return t("time.ago_min", { n: Math.floor(s / 60) });
  if (s < 86400) return t("time.ago_hour", { n: Math.floor(s / 3600) });
  return t("time.ago_day", { n: Math.floor(s / 86400) });
}

function durationText(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  if (m < 60) return t("time.min", { n: m });
  if (m < 1440) return [t("time.hour", { n: Math.floor(m / 60) }), m % 60 ? t("time.min", { n: m % 60 }) : ""].filter(Boolean).join(" ");
  return [t("time.day", { n: Math.floor(m / 1440) }), Math.floor(m / 60) % 24 ? t("time.hour", { n: Math.floor(m / 60) % 24 }) : ""].filter(Boolean).join(" ");
}

function clockText(date) {
  const now = apiNow();
  const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
  const hm = pad(date.getHours()) + ":" + pad(date.getMinutes());
  if (dayKey(date) === dayKey(now)) return t("time.today_at", { time: hm });
  if (dayKey(date) === dayKey(tomorrow)) return t("time.tomorrow_at", { time: hm });
  return t("time.day_at", { day: dayLabel((date.getDay() + 6) % 7), time: hm });
}

// İşin bir sonraki çalışma zamanı (servisin zamanlama kuralıyla aynı). Zamanı geçmiş ama çalışmamışsa: şimdi
function nextRun(schedule, lastRunIso) {
  const now = apiNow();
  const lastRun = lastRunIso ? new Date(lastRunIso) : null;
  if (schedule.type === "interval") {
    if (!lastRun) return now;
    const next = new Date(lastRun.getTime() + schedule.minutes * 60000);
    return next < now ? now : next;
  }
  const [h, m] = schedule.time.split(":").map(Number);
  for (let back = 0; back < 8; back++) {
    const day = new Date(now); day.setDate(now.getDate() + back); day.setHours(h, m, 0, 0);
    if (schedule.type === "weekly" && !schedule.days.includes(DAYS[(day.getDay() + 6) % 7])) continue;
    if (day > now) return day;
    if (!lastRun || dayKey(lastRun) !== dayKey(day)) return now;   // bugünün saati geçmiş, bugün çalışmamış
  }
  return null;
}

function nextRunText(schedule, lastRunIso) {
  const next = nextRun(schedule, lastRunIso);
  if (!next) return "—";
  return next - apiNow() < 60000 ? t("time.now") : clockText(next);
}

function machineState(id) {
  const row = statuses[id];
  const seen = row ? new Date(row.updated_at) : null;
  const online = seen && (apiNow() - seen) / 1000 < ONLINE_SECONDS;
  const offlineProblem = overview && overview.problems.some((p) => p.machine_id === id && p.kind === "offline");
  const running = row && online && row.status.jobs.some((j) => j.state === "running");
  return { row, seen, online, offlineProblem, running };
}

function renderOverview() {
  if (!overview) return;
  const ids = Object.keys(machines).sort((a, b) => machines[a].localeCompare(machines[b]));
  const problemJobs = new Set(overview.problems.filter((p) => p.job).map((p) => p.machine_id + "|" + p.job));

  // Özet cümle (üst bantta)
  const online = ids.filter((id) => machineState(id).online).length;
  const okJobs = jobs.filter((j) => !problemJobs.has(j.machine_id + "|" + jobKey(j)) && machineState(j.machine_id).online).length;
  const today = dayKey(apiNow());
  const todayRuns = overview.runs.filter((r) => r.finished.slice(0, 10) === today);
  const runningCount = ids.reduce((n, id) => n + (machineState(id).online && statuses[id]
    ? statuses[id].status.jobs.filter((j) => j.state === "running").length : 0), 0);
  let summary = ids.length
    ? t("overview.summary", { machines: ids.length, online: possessive(online), jobs: jobs.length, ok: possessive(okJobs) })
    : t("overview.summary_empty");
  if (ids.length) {
    summary += " " + t("overview.summary_today", { total: todayRuns.length + runningCount, ok: todayRuns.filter((r) => !r.failed).length,
      failed: todayRuns.filter((r) => r.failed).length, running: runningCount });
  }
  if (!$("overview-view").hidden) $("page-desc").textContent = summary;

  // Açık sorunlar
  $("ov-prob-note").textContent = overview.problems.length ? t("overview.problems_note") : "";
  $("ov-problems").innerHTML = overview.problems.length
    ? overview.problems.map((p) => `
      <div class="prob">
        <i></i>
        <div><b>${esc(p.machine_name)} · ${esc(t("problem." + p.kind + ".title"))}</b>
          <span>${esc([p.job, problemDetail(p.kind, p.params)].filter(Boolean).join(" · "))}</span></div>
        <small>${esc(durationText(apiNow() - new Date(p.opened_at)))}</small>
      </div>`).join("")
    : `<div class="empty">${esc(t("overview.problems_empty"))}</div>`;

  // Makineler
  $("ov-machines").innerHTML = ids.length
    ? ids.map((id) => {
        const st = machineState(id);
        const count = jobs.filter((j) => j.machine_id === id).length;
        const active = st.running ? st.row.status.active.length : 0;
        const meta = [st.row && st.row.status.os, t("jobs.count", { n: count }),
          st.offlineProblem ? t("overview.unreachable") : !st.online ? t("overview.offline") : active ? t("overview.copying", { n: active }) : ""]
          .filter(Boolean).join(" · ");
        return `<div class="mach">
          <span class="dot ${st.online ? "" : "off"}"></span>
          <span class="nm">${esc(machines[id])}</span>
          <span class="st">${esc(st.seen ? agoText(st.seen) : t("overview.never_seen"))}</span>
          <span class="meta">${esc(meta)}</span>
        </div>`;
      }).join("")
    : `<div class="empty">${th("overview.machines_empty", { button: `<b>${esc(t("overview.add_machine"))}</b>` })}</div>`;

  renderRuns(ids, today);
  renderEvents();
}

function renderRuns(ids, today) {
  const day = ovDay || today;
  const d = new Date(day + "T00:00:00");
  $("ov-day").textContent = day === today ? t("time.today") : t("date.day_month", { day: d.getDate(), month: monthName(d.getMonth()) });
  $("ov-next").disabled = day >= today;
  $("ov-daynav").hidden = ovMode !== "day";
  const legend = (cls, key) => `<span><i class="lg ${cls}"></i>${esc(t(key))}</span>`;
  $("ov-legend").innerHTML = legend("ok", "overview.legend_ok") + legend("bad", "overview.legend_failed")
    + legend("live", "overview.legend_running") + (ovMode === "day" ? legend("plan", "overview.legend_planned") : legend("none", "overview.legend_none"));

  if (!jobs.length) { $("ov-runs").innerHTML = `<div class="empty">${th("overview.runs_empty", { button: `<b>${esc(t("overview.add_job"))}</b>` })}</div>`; return; }
  const groups = ids.filter((id) => jobs.some((j) => j.machine_id === id));
  const lane = (j, cells) => `<div class="lane"><span class="lbl mono" title="${esc(jobKey(j))}">${esc(j.source)}</span>${cells}</div>`;
  const head = (id) => {
    const st = machineState(id);
    return `<div class="grp"><span class="dot ${st.online ? "" : "off"}"></span>${esc(machines[id])}${st.offlineProblem ? " · " + esc(t("overview.unreachable")) : ""}</div>`;
  };

  if (ovMode === "14") {
    const days = [...Array(14)].map((_, i) => { const x = new Date(apiNow()); x.setDate(x.getDate() - 13 + i); return dayKey(x); });
    const header = `<div class="lane hours"><span></span><div class="cells">${days.map((k) => `<span>${Number(k.slice(8))}</span>`).join("")}</div></div>`;
    $("ov-runs").innerHTML = header + groups.map((id) => head(id) + jobs.filter((j) => j.machine_id === id).map((j) => {
      const runs = overview.runs.filter((r) => r.machine_id === id && r.job === jobKey(j));
      const st = statuses[id] && statuses[id].status.jobs.find((x) => x.source === j.source && x.target === j.target);
      const cells = days.map((k) => {
        const dayRuns = runs.filter((r) => r.finished.slice(0, 10) === k);
        let cls = "none";
        if (dayRuns.length) cls = dayRuns[dayRuns.length - 1].failed ? "bad" : "ok";
        if (k === today && st && st.state === "running" && machineState(id).online) cls = "live";
        const title = dayRuns.length ? t("overview.day_runs", { date: k, n: dayRuns.length }) : t("overview.day_no_runs", { date: k });
        return `<b class="${cls}" title="${esc(title)}"></b>`;
      }).join("");
      return lane(j, `<div class="cells">${cells}</div>`);
    }).join("")).join("");
    return;
  }

  // Günün zaman çizelgesi
  const start = new Date(day + "T00:00:00").getTime();
  const span = 86400000;
  const pct = (t) => Math.min(100, Math.max(0, (t - start) / span * 100));
  const now = apiNow().getTime();
  const hours = [...Array(12)].map((_, i) => `<span>${pad(i * 2)}</span>`).join("");
  let html = `<div class="lane hours"><span></span><div class="ticks">${hours}</div></div>`;
  html += groups.map((id) => head(id) + jobs.filter((j) => j.machine_id === id).map((j) => {
    let blocks = overview.runs
      .filter((r) => r.machine_id === id && r.job === jobKey(j))
      .map((r) => ({ a: new Date(r.started).getTime(), b: new Date(r.finished).getTime(), r }))
      .filter((x) => x.b >= start && x.a < start + span)
      .map((x) => `<i class="run ${x.r.failed ? "bad" : "ok"}" style="left:${pct(x.a)}%;width:${Math.max(0.4, pct(x.b) - pct(x.a))}%"
        title="${esc(t(x.r.failed ? "overview.run_title_failed" : "overview.run_title",
          { start: timeText(x.r.started).slice(11, 16), end: timeText(x.r.finished).slice(11, 16), files: x.r.files, failed: x.r.failed }))}"></i>`)
      .join("");
    const st = statuses[id] && statuses[id].status.jobs.find((x) => x.source === j.source && x.target === j.target);
    if (day === today && st && st.state === "running" && st.last_run && machineState(id).online) {
      const a = new Date(st.last_run).getTime();
      blocks += `<i class="run live" style="left:${pct(a)}%;width:${Math.max(0.6, pct(now) - pct(a))}%" title="${esc(t("overview.running_title"))}"></i>`;
    }
    if (day === today) {
      const next = nextRun(j.schedule, st && st.last_run);
      if (next && next.getTime() - now > 60000 && dayKey(next) === today) {   // zamanı gelmiş olanlar "planlı" gösterilmez
        blocks += `<i class="run plan" style="left:${pct(next.getTime())}%" title="${esc(t("overview.planned_title", { time: pad(next.getHours()) + ":" + pad(next.getMinutes()) }))}"></i>`;
      }
    }
    return lane(j, `<div class="track">${blocks}</div>`);
  }).join("")).join("");
  if (day === today) html += `<div class="now" style="--x:${pct(now)}"><b>${pad(apiNow().getHours())}:${pad(apiNow().getMinutes())}</b></div>`;
  $("ov-runs").innerHTML = `<div class="tl">${html}</div>`;
}

const EVENT_BAD = new Set(["problem", "run_failed"]);

// Sorunun kısa açıklaması: dil dosyasındaki "problem.<tür>.detail" yazısı (varsa) + hatanın kendisi (varsa)
function problemDetail(kind, params) {
  const values = { ...params };
  for (const name of ["due", "last_seen"]) if (values[name]) values[name] = dateTimeText(new Date(values[name]));
  const key = "problem." + kind + ".detail";
  return [key in TEXTS ? t(key, values) : "", capitalize(errorText(params.error))].filter(Boolean).join(" · ");
}

// Olayın cümlesi: sorun açıldı / düzeldi ise sorunun başlığı, değilse "event.<tür>"
function eventText(e) {
  if (e.kind === "problem") return t("problem." + e.params.problem + ".title");
  if (e.kind === "resolved") return t("problem." + e.params.problem + ".resolved");
  return t("event." + e.kind, e.params);
}

// Olayın altındaki açıklama
function eventDetail(e) {
  if (e.kind === "problem") return e.job || problemDetail(e.params.problem, e.params);
  if (e.kind === "resolved") return [e.job, t("event.problem_duration", { duration: problemDuration(e.params.seconds) })].filter(Boolean).join(" · ");
  if (e.kind === "restore") return e.params.path;
  return e.job;
}

// Sorunun ne kadar sürdüğü: "3 saat 51 dakika", "2 gün 4 saat", "1 dakikadan az"
function problemDuration(seconds) {
  let minutes = Math.floor(seconds / 60);
  const days = Math.floor(minutes / 1440); minutes %= 1440;
  const hours = Math.floor(minutes / 60); minutes %= 60;
  const parts = [days ? t("duration.day", { n: days }) : "", hours ? t("duration.hour", { n: hours }) : "",
    minutes && !days ? t("duration.minute", { n: minutes }) : ""].filter(Boolean);
  return parts.join(" ") || t("duration.less_than_minute");
}

// Tarih ve saat: "23.11.2026 14:05"
function dateTimeText(d) {
  return t("date.datetime", { day: pad(d.getDate()), month: pad(d.getMonth() + 1), year: d.getFullYear(),
    time: pad(d.getHours()) + ":" + pad(d.getMinutes()) });
}
function renderEvents() {
  const day = ovDay || dayKey(apiNow());
  const d = new Date(day + "T00:00:00");
  $("ov-events-day").textContent = day === dayKey(apiNow()) ? t("time.today_lower")
    : t("date.day_month_weekday", { day: d.getDate(), month: monthName(d.getMonth()), weekday: weekdayName(d.getDay()) });
  $("ov-events").innerHTML = overview.events.length
    ? overview.events.map((e) => `
      <div class="ev ${EVENT_BAD.has(e.kind) ? "bad" : ""}">
        <time>${esc(e.time.slice(11, 16))}</time>
        <div><b>${esc(eventText(e))}</b><span>${esc([e.machine_name, eventDetail(e)].filter(Boolean).join(" · "))}</span></div>
      </div>`).join("")
    : `<div class="empty">${esc(t("overview.events_empty"))}</div>`;
}

document.querySelectorAll('input[name="ov-mode"]').forEach((input) => input.addEventListener("change", () => {
  ovMode = input.value;
  renderOverview();
}));
$("ov-prev").addEventListener("click", () => {
  const d = new Date((ovDay || dayKey(apiNow())) + "T00:00:00"); d.setDate(d.getDate() - 1);
  ovDay = dayKey(d); loadOverview();
});
$("ov-next").addEventListener("click", () => {
  const d = new Date((ovDay || dayKey(apiNow())) + "T00:00:00"); d.setDate(d.getDate() + 1);
  ovDay = dayKey(d) >= dayKey(apiNow()) ? null : dayKey(d); loadOverview();
});
$("ov-add-machine").addEventListener("click", () => showTab("machines"));
$("ov-add-job").addEventListener("click", () => { showTab("jobs"); openForm(true); });