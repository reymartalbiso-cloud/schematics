(() => {
  "use strict";

  // ---------------------------------------------------------------
  // State — currentSpec lives in the browser only; every request to
  // the backend sends it back as current_spec. There is no server
  // session store.
  // ---------------------------------------------------------------
  const state = {
    view: "dashboard",
    mode: "floorplan",
    currentSpec: null,
    currentDesignId: null,
    currentTitle: "",
    dirty: false,
    revision: 0,
    librecadInstalled: false,
    chat: [], // {role: 'user'|'assistant'|'error', text}
    zoom: { scale: 1, tx: 0, ty: 0 },
    // Container mode only - which sheets to draw. Defaults to plan-only;
    // the full 4-view sheet is opt-in via the "Generate elevations too"
    // action so a single edit doesn't force-render all 4 views every time.
    views: ["plan"],
    // When the backend asks a clarifying question, we stash the request it
    // was answering here so the user's reply is sent WITH that context (the
    // backend is stateless and only sees one prompt string at a time).
    pendingClarification: null,
  };

  const ALL_CONTAINER_VIEWS = ["plan", "front", "side", "back"];

  const EXAMPLES = {
    floorplan: [
      "A 6m x 4m living room, door on the south wall, two windows",
      "Add a 3m x 3.5m bedroom next to it",
      "Move the door 1m to the left",
    ],
    container: [
      "20ft container home with a kitchen run and two sliding glass doors",
      "40ft container office, no kitchen",
      "Add a fold-out platform on the side",
    ],
  };

  // ---------------------------------------------------------------
  // DOM refs
  // ---------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  const el = {
    dashboardView: $("dashboard-view"),
    editorView: $("editor-view"),
    dashboardActions: $("dashboard-actions"),
    editorActions: $("editor-actions"),
    dashboardEmpty: $("dashboard-empty"),
    dashboardGrid: $("dashboard-grid"),
    exampleChipsEmpty: $("example-chips-empty"),
    exampleChipsEditor: $("example-chips-editor"),
    onboardingNote: $("onboarding-note"),
    saveIndicator: $("save-indicator"),
    designTitle: $("design-title"),
    modeButtons: Array.from(document.querySelectorAll(".mode-btn")),
    chatLog: $("chat-log"),
    chatForm: $("chat-form"),
    chatInput: $("chat-input"),
    chatSend: $("chat-send"),
    previewImg: $("preview-img"),
    previewEmpty: $("preview-empty"),
    previewWrap: $("preview-canvas-wrap"),
    plotSweep: $("plot-sweep"),
    titleblock: {
      dwg: $("tb-dwg"),
      mode: $("tb-mode"),
      scale: $("tb-scale"),
      rev: $("tb-rev"),
      date: $("tb-date"),
    },
    toast: $("toast"),
    memoryBackdrop: $("memory-modal-backdrop"),
    prefList: $("pref-list"),
    historyList: $("history-list"),
    prefAddForm: $("pref-add-form"),
    prefAddInput: $("pref-add-input"),
    saveBackdrop: $("save-modal-backdrop"),
    saveForm: $("save-modal-form"),
    saveInput: $("save-modal-input"),
    intakeBackdrop: $("intake-modal-backdrop"),
    intakeForm: $("intake-form"),
    intakeFields: $("intake-fields"),
    intakeTitle: $("intake-modal-title"),
  };

  // ---------------------------------------------------------------
  // API helpers
  // ---------------------------------------------------------------
  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: options.body ? { "Content-Type": "application/json" } : {},
      ...options,
    });
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("authentication required");
    }
    if (!resp.ok) {
      const isJson = (resp.headers.get("content-type") || "").includes("application/json");
      const data = isJson ? await resp.json() : null;
      throw new Error((data && data.error) || `Request failed (${resp.status})`);
    }
    if (options.expect === "blob") return resp.blob();
    const isJson = (resp.headers.get("content-type") || "").includes("application/json");
    return isJson ? resp.json() : null;
  }

  const postJSON = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });
  const postForBlob = (path, body) =>
    api(path, { method: "POST", body: JSON.stringify(body), expect: "blob" });

  // ---------------------------------------------------------------
  // Modal wiring (shared by the memory + save-title modals)
  // ---------------------------------------------------------------
  const modalBackdrops = [];
  function wireModal(backdrop, closeBtnId) {
    modalBackdrops.push(backdrop);
    $(closeBtnId).addEventListener("click", () => { backdrop.hidden = true; });
    backdrop.addEventListener("click", (e) => { if (e.target === backdrop) backdrop.hidden = true; });
  }

  // ---------------------------------------------------------------
  // Toast
  // ---------------------------------------------------------------
  let toastTimer = null;
  function toast(message, kind = "info") {
    el.toast.textContent = message;
    el.toast.dataset.kind = kind;
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 3600);
  }

  // ---------------------------------------------------------------
  // View switching
  // ---------------------------------------------------------------
  function showView(view) {
    state.view = view;
    el.dashboardView.hidden = view !== "dashboard";
    el.editorView.hidden = view !== "editor";
    el.dashboardActions.hidden = view !== "dashboard";
    el.editorActions.hidden = view !== "editor";
    if (view === "dashboard") loadDashboard();
  }

  // ---------------------------------------------------------------
  // Dashboard
  // ---------------------------------------------------------------
  async function loadDashboard() {
    let list = [];
    try {
      list = await api("/api/designs");
    } catch (err) {
      toast(err.message, "error");
    }
    el.dashboardGrid.innerHTML = "";
    if (!list.length) {
      el.dashboardEmpty.hidden = false;
      el.dashboardGrid.hidden = true;
      return;
    }
    el.dashboardEmpty.hidden = true;
    el.dashboardGrid.hidden = false;
    list.forEach((meta, i) => el.dashboardGrid.appendChild(renderDesignCard(meta, i)));
  }

  function renderDesignCard(meta, index) {
    const card = document.createElement("div");
    card.className = "design-card";
    card.style.animationDelay = `${Math.min(index * 30, 300)}ms`;
    card.innerHTML = `
      <div class="design-thumb-empty" data-thumb>Loading preview…</div>
      <div class="design-card-body">
        <p class="design-card-title">${escapeHtml(meta.title)}</p>
        <div class="design-card-meta">
          <span class="mode-badge">${meta.mode === "container" ? "Container Home" : "Floor Plan"}</span>
          <span>${formatDate(meta.updated_at)}</span>
        </div>
      </div>
      <div class="design-card-actions">
        <button type="button" data-action="delete">Delete</button>
      </div>
    `;
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-action='delete']")) return;
      openDesign(meta.id);
    });
    card.querySelector("[data-action='delete']").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!window.confirm(`Delete "${meta.title}"? This can't be undone.`)) return;
      try {
        await api(`/api/designs/${meta.id}`, { method: "DELETE" });
        loadDashboard();
      } catch (err) {
        toast(err.message, "error");
      }
    });
    fetchThumb(meta.id, card.querySelector("[data-thumb]"));
    return card;
  }

  async function fetchThumb(id, thumbEl) {
    try {
      const record = await api(`/api/designs/${id}`);
      const img = document.createElement("img");
      img.className = "design-thumb";
      img.alt = record.title;
      img.src = record.preview_data_uri;
      thumbEl.replaceWith(img);
    } catch {
      thumbEl.textContent = "Preview unavailable";
    }
  }

  async function openDesign(id) {
    try {
      const record = await api(`/api/designs/${id}`);
      resetEditorState(record.mode);
      state.currentDesignId = record.id;
      state.currentTitle = record.title;
      state.currentSpec = record.spec;
      state.dirty = false;
      el.designTitle.value = record.title;
      setPreview(record.preview_data_uri);
      setSaveIndicator(true);
      addMessage("assistant", `Reopened "${record.title}". Keep editing conversationally.`);
      dismissOnboarding(true);
      renderTitleblock();
      updateElevationsButton();
      updateSaveAsButton();
      showView("editor");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function formatDate(iso) {
    try {
      return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    } catch {
      return iso;
    }
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ---------------------------------------------------------------
  // Editor: new design / mode switching
  // ---------------------------------------------------------------
  function resetEditorState(mode) {
    state.mode = mode;
    state.currentSpec = null;
    state.currentDesignId = null;
    state.currentTitle = "";
    state.dirty = false;
    state.revision = 0;
    state.chat = [];
    state.zoom = { scale: 1, tx: 0, ty: 0 };
    state.views = ["plan"];
    state.pendingClarification = null;
    el.chatLog.innerHTML = "";
    el.designTitle.value = "";
    setPreview(null);
    setSaveIndicator(false);
    updateModeButtons();
    updateElevationsButton();
    updateSaveAsButton();
    renderExampleChips();
    renderTitleblock();
  }

  function startNewDesign(mode) {
    resetEditorState(mode);
    showView("editor");
    openIntake(mode);
  }

  // ---------------------------------------------------------------
  // Structured intake — a short fixed questionnaire before the chat,
  // front-loading the handful of things that most shape a first draft.
  // Skippable; composes with the on-demand clarification mechanism.
  // ---------------------------------------------------------------
  const CONTAINER_SIZES = {
    "20ft": "a 20ft container (6058mm long, 2438mm wide, 2896mm high)",
    "40ft": "a 40ft container (12192mm long, 2438mm wide, 2896mm high)",
  };

  function intakeFieldsHtml(mode) {
    if (mode === "container") {
      return `
      <div class="intake-q">
        <span class="intake-label">Container size</span>
        <div class="intake-options">
          <label class="intake-opt is-on"><input type="radio" name="size" value="20ft" checked> 20ft (6058mm)</label>
          <label class="intake-opt"><input type="radio" name="size" value="40ft"> 40ft (12192mm)</label>
          <label class="intake-opt" data-reveal="in-size-custom"><input type="radio" name="size" value="custom"> Custom</label>
        </div>
        <input class="intake-number" id="in-size-custom" hidden placeholder="e.g. 9000" style="margin-top:8px;width:200px;" aria-label="Custom length in mm">
      </div>
      <div class="intake-q">
        <span class="intake-label">What's it for?</span>
        <div class="intake-options">
          <label class="intake-opt is-on"><input type="radio" name="purpose" value="home" checked> Home</label>
          <label class="intake-opt"><input type="radio" name="purpose" value="office"> Office</label>
          <label class="intake-opt"><input type="radio" name="purpose" value="studio"> Studio</label>
          <label class="intake-opt"><input type="radio" name="purpose" value="cafe"> Cafe</label>
          <label class="intake-opt" data-reveal="in-purpose-custom"><input type="radio" name="purpose" value="other"> Other</label>
        </div>
        <input class="intake-text" id="in-purpose-custom" hidden placeholder="describe the use" style="margin-top:8px;">
      </div>
      <div class="intake-q">
        <span class="intake-label">Must-haves</span>
        <div class="intake-options">
          <label class="intake-opt"><input type="checkbox" name="must" value="kitchen"> Kitchen</label>
          <label class="intake-opt"><input type="checkbox" name="must" value="bathroom"> Bathroom</label>
          <label class="intake-opt" data-reveal="in-bed-wrap"><input type="checkbox" name="must" value="bedrooms"> Bedroom(s)</label>
          <label class="intake-opt"><input type="checkbox" name="must" value="living"> Living area</label>
          <label class="intake-opt"><input type="checkbox" name="must" value="deck"> Deck / balcony</label>
        </div>
        <div class="intake-inline" id="in-bed-wrap" hidden style="margin-top:8px;">
          <span class="intake-sub">How many bedrooms?</span>
          <input class="intake-number" id="in-bed-count" type="number" min="1" max="6" value="2" aria-label="Number of bedrooms">
        </div>
      </div>
      <div class="intake-q">
        <span class="intake-label">Main entry</span>
        <div class="intake-options">
          <label class="intake-opt is-on"><input type="radio" name="entry" value="front centre" checked> Front — centre</label>
          <label class="intake-opt"><input type="radio" name="entry" value="front left"> Front — left</label>
          <label class="intake-opt"><input type="radio" name="entry" value="front right"> Front — right</label>
          <label class="intake-opt"><input type="radio" name="entry" value=""> Not sure</label>
        </div>
      </div>
      <div class="intake-q">
        <span class="intake-label">Anything else?</span>
        <textarea class="intake-text" id="in-notes" rows="2" placeholder="finishes, special features, constraints…"></textarea>
      </div>`;
    }
    // floorplan
    return `
      <div class="intake-q">
        <span class="intake-label">Overall size</span>
        <input class="intake-text" id="in-size" placeholder="e.g. 6m x 4m" style="max-width:220px;">
      </div>
      <div class="intake-q">
        <span class="intake-label">What's the space?</span>
        <div class="intake-options">
          <label class="intake-opt is-on"><input type="radio" name="purpose" value="room" checked> Single room</label>
          <label class="intake-opt"><input type="radio" name="purpose" value="house"> House</label>
          <label class="intake-opt"><input type="radio" name="purpose" value="office"> Office</label>
          <label class="intake-opt" data-reveal="in-purpose-custom"><input type="radio" name="purpose" value="other"> Other</label>
        </div>
        <input class="intake-text" id="in-purpose-custom" hidden placeholder="describe the use" style="margin-top:8px;">
      </div>
      <div class="intake-q">
        <span class="intake-label">Rooms needed</span>
        <input class="intake-text" id="in-rooms" placeholder="e.g. living room, kitchen, bathroom">
      </div>
      <div class="intake-q">
        <span class="intake-label">Main door wall</span>
        <div class="intake-options">
          <label class="intake-opt is-on"><input type="radio" name="door" value="south" checked> South</label>
          <label class="intake-opt"><input type="radio" name="door" value="north"> North</label>
          <label class="intake-opt"><input type="radio" name="door" value="east"> East</label>
          <label class="intake-opt"><input type="radio" name="door" value="west"> West</label>
        </div>
      </div>
      <div class="intake-q">
        <span class="intake-label">Anything else?</span>
        <textarea class="intake-text" id="in-notes" rows="2" placeholder="fixtures, constraints…"></textarea>
      </div>`;
  }

  function openIntake(mode) {
    el.intakeTitle.textContent = mode === "container" ? "New Container Home" : "New Floor Plan";
    el.intakeFields.innerHTML = intakeFieldsHtml(mode);
    el.intakeBackdrop.hidden = false;
  }

  // Recompute chip highlight + conditional reveals whenever an input changes.
  el.intakeFields.addEventListener("change", () => {
    el.intakeFields.querySelectorAll(".intake-opt").forEach((opt) => {
      const input = opt.querySelector("input");
      opt.classList.toggle("is-on", input.checked);
      const revId = opt.dataset.reveal;
      if (revId) {
        const target = document.getElementById(revId);
        if (target) target.hidden = !input.checked;
      }
    });
  });

  function intakeVal(sel) {
    const node = el.intakeFields.querySelector(sel);
    return node ? node.value.trim() : "";
  }

  function assembleIntakePrompt(mode) {
    if (mode === "container") {
      const size = intakeVal('input[name="size"]:checked') || "20ft";
      let sizeStr = CONTAINER_SIZES[size];
      if (size === "custom") {
        const mm = intakeVal("#in-size-custom");
        sizeStr = mm ? `a container ${mm}mm long, 2438mm wide, 2896mm high` : "a container";
      }
      let purpose = intakeVal('input[name="purpose"]:checked') || "home";
      if (purpose === "other") purpose = intakeVal("#in-purpose-custom") || "space";
      const labels = { kitchen: "a kitchen", bathroom: "a bathroom", living: "a living area", deck: "a deck / balcony" };
      const must = Array.from(el.intakeFields.querySelectorAll('input[name="must"]:checked')).map((i) => {
        if (i.value === "bedrooms") { const n = intakeVal("#in-bed-count") || "2"; return `${n} bedroom${n === "1" ? "" : "s"}`; }
        return labels[i.value];
      });
      const entry = intakeVal('input[name="entry"]:checked');
      const notes = intakeVal("#in-notes");

      let p = `${sizeStr.charAt(0).toUpperCase()}${sizeStr.slice(1)} used as a ${purpose}.`;
      if (must.length) p += ` It must include ${joinList(must)}.`;
      if (entry) {
        const where = entry.replace("front ", "");
        p += ` The main entry is a sliding glass door on the front wall${where === "centre" ? ", centred" : ", toward the " + where}.`;
      }
      if (notes) p += ` Additional notes: ${notes}.`;
      return p;
    }
    // floorplan
    const size = intakeVal("#in-size");
    let purpose = intakeVal('input[name="purpose"]:checked') || "room";
    if (purpose === "other") purpose = intakeVal("#in-purpose-custom") || "space";
    const rooms = intakeVal("#in-rooms");
    const door = intakeVal('input[name="door"]:checked');
    const notes = intakeVal("#in-notes");
    let p = `A floor plan for a ${purpose}${size ? `, ${size}` : ""}.`;
    if (rooms) p += ` Rooms: ${rooms}.`;
    if (door) p += ` Main entry door on the ${door} wall.`;
    p += " Include dimension lines on the walls.";
    if (notes) p += ` Additional notes: ${notes}.`;
    return p;
  }

  function joinList(items) {
    if (items.length <= 1) return items[0] || "";
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }

  el.intakeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const prompt = assembleIntakePrompt(state.mode);
    el.intakeBackdrop.hidden = true;
    submitPrompt(prompt, "Set up a new " + (state.mode === "container" ? "container home" : "floor plan") + " (via the quick intake).");
  });
  $("intake-skip").addEventListener("click", () => {
    el.intakeBackdrop.hidden = true;
    el.chatInput.focus();
  });
  $("intake-modal-close").addEventListener("click", () => {
    el.intakeBackdrop.hidden = true;
    el.chatInput.focus();
  });
  el.intakeBackdrop.addEventListener("click", (e) => {
    if (e.target === el.intakeBackdrop) el.intakeBackdrop.hidden = true;
  });
  modalBackdrops.push(el.intakeBackdrop);  // Escape closes it too (acts as skip)

  function updateModeButtons() {
    el.modeButtons.forEach((btn) => {
      btn.setAttribute("aria-selected", String(btn.dataset.mode === state.mode));
    });
  }

  function updateElevationsButton() {
    const btn = $("btn-elevations");
    btn.hidden = state.mode !== "container";
    btn.disabled = !state.currentSpec;
    const showingAll = state.views.length > 1;
    btn.textContent = showingAll ? "Plan view only" : "Generate elevations too";
  }

  function requestModeSwitch(newMode) {
    if (newMode === state.mode) return;
    if (state.currentSpec && state.dirty) {
      const ok = window.confirm(
        "Switching mode starts a new drawing and discards your unsaved changes. Continue?"
      );
      if (!ok) return;
    } else if (state.currentSpec) {
      const ok = window.confirm("Switching mode starts a new drawing. Continue?");
      if (!ok) return;
    }
    resetEditorState(newMode);
  }

  // ---------------------------------------------------------------
  // Onboarding + example chips
  // ---------------------------------------------------------------
  function hasGeneratedBefore() {
    return localStorage.getItem("draftboard_has_generated") === "1";
  }
  function markHasGenerated() {
    localStorage.setItem("draftboard_has_generated", "1");
  }
  function dismissOnboarding(permanent) {
    el.onboardingNote.hidden = true;
    if (permanent) markHasGenerated();
  }
  function renderExampleChips() {
    const chipsHtml = (mode) =>
      EXAMPLES[mode]
        .map((text) => `<button type="button" class="chip" data-text="${escapeHtml(text)}">${escapeHtml(text)}</button>`)
        .join("");
    el.exampleChipsEditor.innerHTML = chipsHtml(state.mode);
    el.exampleChipsEmpty.innerHTML = [
      ...EXAMPLES.floorplan.slice(0, 1).map((t) => ({ mode: "floorplan", t })),
      ...EXAMPLES.container.slice(0, 1).map((t) => ({ mode: "container", t })),
    ]
      .map(
        ({ mode, t }) =>
          `<button type="button" class="chip" data-mode="${mode}" data-text="${escapeHtml(t)}">${escapeHtml(t)}</button>`
      )
      .join("");
    el.onboardingNote.hidden = hasGeneratedBefore();
  }

  el.exampleChipsEditor.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    el.chatInput.value = chip.dataset.text;
    el.chatInput.focus();
  });
  el.exampleChipsEmpty.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    startNewDesign(chip.dataset.mode);
    el.chatInput.value = chip.dataset.text;
    el.chatInput.focus();
  });
  $("onboarding-dismiss").addEventListener("click", () => dismissOnboarding(true));

  // ---------------------------------------------------------------
  // Chat + generation
  // ---------------------------------------------------------------
  function addMessage(role, text) {
    state.chat.push({ role, text });
    const div = document.createElement("div");
    div.className = `msg msg-${role}`;
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent = role === "user" ? "YOU" : role === "error" ? "ERROR" : "DRAFTBOARD";
    div.appendChild(meta);
    const body = document.createElement("div");
    body.textContent = text;
    div.appendChild(body);
    el.chatLog.appendChild(div);
    el.chatLog.scrollTop = el.chatLog.scrollHeight;
    return div;
  }

  function clearPending(pendingMsg) {
    pendingMsg.remove();
    state.chat.pop();
  }

  el.chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = el.chatInput.value.trim();
    if (!text) return;
    el.chatInput.value = "";
    submitPrompt(text);
  });

  // Shared generation path used by both the chat box and the structured
  // intake. `userLabel` is what gets echoed in the chat as the user's turn
  // (defaults to the sent text; the intake shows a friendly summary instead
  // of the long assembled prompt).
  async function submitPrompt(text, userLabel) {
    addMessage("user", userLabel || text);
    dismissOnboarding(false);
    el.chatSend.disabled = true;
    el.plotSweep.hidden = false;

    // If we're answering a clarifying question, send the original request
    // together with this answer so the stateless backend has the full context.
    const effectiveText = state.pendingClarification
      ? `${state.pendingClarification}\n\n(Clarification: ${text})`
      : text;

    const pendingMsg = addMessage("assistant", state.currentSpec ? "Applying your edit…" : "Generating your design…");
    pendingMsg.classList.add("msg-pending");

    try {
      const data = await postJSON("/api/prompt", {
        mode: state.mode,
        text: effectiveText,
        current_spec: state.currentSpec,
        views: state.mode === "container" ? state.views : undefined,
      });

      if (data.needs_clarification) {
        // Not a failure - a question. Keep the drawing untouched and wait
        // for the user's reply, remembering what they're answering.
        state.pendingClarification = effectiveText;
        clearPending(pendingMsg);
        addMessage("assistant", data.question);
        return;
      }

      state.pendingClarification = null;
      state.currentSpec = data.spec;
      state.librecadInstalled = data.librecad_installed;
      state.dirty = true;
      state.revision += 1;
      markHasGenerated();
      setSaveIndicator(false);
      setPreview(data.preview_data_uri);
      renderTitleblock();
      updateLibrecadButton();
      updateElevationsButton();
      clearPending(pendingMsg);
      addMessage("assistant", "Updated the preview — take a look, or keep iterating.");
    } catch (err) {
      // Errors are a next step, not a dead end: the message is specific
      // (validation problems, fit conflicts) and the user can adjust and retry.
      state.pendingClarification = null;
      clearPending(pendingMsg);
      addMessage("error", err.message);
    } finally {
      el.chatSend.disabled = false;
      el.plotSweep.hidden = true;
    }
  }

  function updateLibrecadButton() {
    $("btn-librecad").disabled = !state.librecadInstalled;
  }

  $("btn-elevations").addEventListener("click", async () => {
    if (!state.currentSpec || state.mode !== "container") return;
    const nextViews = state.views.length > 1 ? ["plan"] : ALL_CONTAINER_VIEWS;
    const btn = $("btn-elevations");
    btn.disabled = true;
    try {
      const data = await postJSON("/api/render", {
        mode: state.mode,
        spec: state.currentSpec,
        views: nextViews,
      });
      state.views = nextViews;
      setPreview(data.preview_data_uri);
      toast(nextViews.length > 1 ? "Generated the full elevation sheet." : "Back to plan view only.");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      updateElevationsButton();
    }
  });

  // ---------------------------------------------------------------
  // Preview + titleblock
  // ---------------------------------------------------------------
  function setPreview(dataUri) {
    if (!dataUri) {
      el.previewImg.hidden = true;
      el.previewImg.removeAttribute("src");
      el.previewEmpty.hidden = false;
      return;
    }
    el.previewEmpty.hidden = true;
    el.previewImg.hidden = false;
    el.previewImg.src = dataUri;
    resetZoom();
  }

  function renderTitleblock() {
    el.titleblock.dwg.textContent = (state.currentTitle || el.designTitle.value || "UNTITLED").toUpperCase();
    el.titleblock.mode.textContent = state.mode === "container" ? "CONTAINER HOME" : "FLOOR PLAN";
    el.titleblock.scale.textContent = state.mode === "container" ? "1:30" : "NTS";
    el.titleblock.rev.textContent = String(state.revision);
    el.titleblock.date.textContent = new Date().toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  el.designTitle.addEventListener("input", () => {
    state.currentTitle = el.designTitle.value;
    renderTitleblock();
    state.dirty = true;
    setSaveIndicator(false);
  });

  function setSaveIndicator(saved) {
    el.saveIndicator.dataset.state = saved ? "saved" : "unsaved";
    el.saveIndicator.querySelector(".save-label").textContent = saved ? "" : "Unsaved";
  }

  // ---------------------------------------------------------------
  // Zoom / pan
  // ---------------------------------------------------------------
  function applyZoomTransform() {
    const { scale, tx, ty } = state.zoom;
    el.previewImg.style.transform = `translate(-50%, -50%) translate(${tx}px, ${ty}px) scale(${scale})`;
    $("zoom-reset").textContent = `${Math.round(scale * 100)}%`;
  }
  function resetZoom() {
    state.zoom = { scale: 1, tx: 0, ty: 0 };
    applyZoomTransform();
  }
  function zoomBy(factor) {
    state.zoom.scale = Math.min(6, Math.max(0.15, state.zoom.scale * factor));
    applyZoomTransform();
  }
  $("zoom-in").addEventListener("click", () => zoomBy(1.25));
  $("zoom-out").addEventListener("click", () => zoomBy(0.8));
  $("zoom-reset").addEventListener("click", resetZoom);
  el.previewWrap.addEventListener(
    "wheel",
    (e) => {
      if (el.previewImg.hidden) return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.1 : 0.9);
    },
    { passive: false }
  );

  let dragging = false;
  let dragStart = null;
  el.previewWrap.addEventListener("pointerdown", (e) => {
    if (el.previewImg.hidden) return;
    dragging = true;
    dragStart = { x: e.clientX, y: e.clientY, tx: state.zoom.tx, ty: state.zoom.ty };
    el.previewWrap.setPointerCapture(e.pointerId);
  });
  el.previewWrap.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    state.zoom.tx = dragStart.tx + (e.clientX - dragStart.x);
    state.zoom.ty = dragStart.ty + (e.clientY - dragStart.y);
    applyZoomTransform();
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach((evt) =>
    el.previewWrap.addEventListener(evt, () => { dragging = false; })
  );

  // ---------------------------------------------------------------
  // Save / download / open in LibreCAD
  // ---------------------------------------------------------------
  $("btn-save").addEventListener("click", async () => {
    if (!state.currentSpec) {
      toast("Generate a design before saving.", "error");
      return;
    }
    if (!state.currentDesignId && !el.designTitle.value.trim()) {
      openSaveModal();
      return;
    }
    await performSave(el.designTitle.value.trim() || "Untitled drawing");
  });

  // When true, the next save-modal submit forks a new record instead of
  // updating the current one ("Save as new").
  let saveAsNewPending = false;

  function openSaveModal(forkMode = false) {
    saveAsNewPending = forkMode;
    const current = el.designTitle.value.trim() || state.currentTitle;
    el.saveInput.value = forkMode && current ? `Copy of ${current}` : current;
    el.saveBackdrop.hidden = false;
    el.saveInput.focus();
    el.saveInput.select();
  }
  wireModal(el.saveBackdrop, "save-modal-close");
  el.saveForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = el.saveInput.value.trim();
    if (!title) return;
    el.saveBackdrop.hidden = true;
    el.designTitle.value = title;
    await performSave(title, saveAsNewPending);
    saveAsNewPending = false;
  });

  async function performSave(title, asNew = false) {
    try {
      const meta = await postJSON("/api/designs", {
        mode: state.mode,
        title,
        spec: state.currentSpec,
        id: asNew ? null : state.currentDesignId,
      });
      state.currentDesignId = meta.id;
      state.currentTitle = meta.title;
      state.dirty = false;
      setSaveIndicator(true);
      renderTitleblock();
      updateSaveAsButton();
      toast(asNew ? "Saved as a new design." : "Saved.");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // "Save as new" forks the current drawing into its own record - only
  // meaningful once the design has been saved at least once.
  function updateSaveAsButton() {
    $("btn-save-as").hidden = !state.currentDesignId;
  }

  $("btn-save-as").addEventListener("click", () => {
    if (!state.currentSpec || !state.currentDesignId) return;
    openSaveModal(true);
  });

  $("btn-download").addEventListener("click", async () => {
    if (!state.currentSpec) {
      toast("Generate a design before downloading.", "error");
      return;
    }
    try {
      const blob = await postForBlob("/api/download", {
        mode: state.mode,
        spec: state.currentSpec,
        views: state.mode === "container" ? state.views : undefined,
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = state.mode === "container" ? "container_home.dxf" : "floorplan.dxf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast(err.message, "error");
    }
  });

  $("btn-librecad").addEventListener("click", async () => {
    if (!state.currentSpec) return;
    try {
      await postJSON("/api/open_librecad", {
        mode: state.mode,
        spec: state.currentSpec,
        views: state.mode === "container" ? state.views : undefined,
      });
      toast("Opened in LibreCAD.");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------------------------------------------------------
  // Memory modal
  // ---------------------------------------------------------------
  async function openMemoryModal() {
    el.memoryBackdrop.hidden = false;
    try {
      const mem = await api("/api/memory");
      renderMemory(mem);
    } catch (err) {
      toast(err.message, "error");
    }
  }
  function renderMemory(mem) {
    el.prefList.innerHTML = "";
    if (!mem.preferences.length) {
      el.prefList.innerHTML = '<li class="empty-hint">No preferences saved yet.</li>';
    } else {
      mem.preferences.forEach((pref, i) => {
        const li = document.createElement("li");
        li.innerHTML = `<span>${escapeHtml(pref)}</span><button class="pref-remove" aria-label="Remove" data-index="${i}">×</button>`;
        el.prefList.appendChild(li);
      });
    }
    const allHistory = [...mem.history.floorplan.map((h) => ({ mode: "Floor Plan", h })), ...mem.history.container.map((h) => ({ mode: "Container", h }))];
    el.historyList.innerHTML = allHistory.length
      ? allHistory.slice(-15).reverse().map((item) => `<li>[${item.mode}] ${escapeHtml(item.h)}</li>`).join("")
      : '<li class="empty-hint">No design history yet.</li>';
  }
  el.prefList.addEventListener("click", async (e) => {
    const btn = e.target.closest(".pref-remove");
    if (!btn) return;
    try {
      const mem = await api(`/api/memory/preference/${btn.dataset.index}`, { method: "DELETE" });
      renderMemory(mem);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  el.prefAddForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = el.prefAddInput.value.trim();
    if (!text) return;
    try {
      const mem = await postJSON("/api/memory/preference", { text });
      el.prefAddInput.value = "";
      renderMemory(mem);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  $("btn-clear-memory").addEventListener("click", async () => {
    if (!window.confirm("Clear all stored preferences and design history? This can't be undone.")) return;
    try {
      const mem = await postJSON("/api/memory/clear", {});
      renderMemory(mem);
      toast("Memory cleared.");
    } catch (err) {
      toast(err.message, "error");
    }
  });
  wireModal(el.memoryBackdrop, "memory-modal-close");
  $("btn-open-memory").addEventListener("click", openMemoryModal);
  $("btn-open-memory-2").addEventListener("click", openMemoryModal);

  // ---------------------------------------------------------------
  // Nav wiring
  // ---------------------------------------------------------------
  $("btn-home").addEventListener("click", () => showView("dashboard"));
  $("btn-back").addEventListener("click", () => {
    if (state.dirty && !window.confirm("You have unsaved changes. Leave without saving?")) return;
    showView("dashboard");
  });
  $("btn-new-floorplan").addEventListener("click", () => startNewDesign("floorplan"));
  $("btn-new-container").addEventListener("click", () => startNewDesign("container"));
  $("empty-btn-floorplan").addEventListener("click", () => startNewDesign("floorplan"));
  $("empty-btn-container").addEventListener("click", () => startNewDesign("container"));
  el.modeButtons.forEach((btn) => btn.addEventListener("click", () => requestModeSwitch(btn.dataset.mode)));

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") modalBackdrops.forEach((backdrop) => { backdrop.hidden = true; });
  });

  // ---------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------
  resetEditorState("floorplan");
  showView("dashboard");
})();
