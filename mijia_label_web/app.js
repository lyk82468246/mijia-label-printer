(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    kind: "text",
    imageData: null,
    previewTimer: null,
    busy: false,
    batchBusy: false,
    batchFileData: null,
    batchFilename: "",
  };

  const elements = {
    status: $("printerStatus"),
    checkButton: $("checkButton"),
    siteQr: $("siteQr"),
    siteUrl: $("siteUrl"),
    devicePaths: $("devicePaths"),
    tabs: $("kindTabs"),
    textInput: $("textInput"),
    qrInput: $("qrInput"),
    barcodeInput: $("barcodeInput"),
    imageInput: $("imageInput"),
    compositeText: $("compositeText"),
    compositeQr: $("compositeQr"),
    compositeBarcode: $("compositeBarcode"),
    margin: $("margin"),
    fontSize: $("fontSize"),
    align: $("align"),
    lineSpacing: $("lineSpacing"),
    threshold: $("threshold"),
    qrError: $("qrError"),
    barcodeFormat: $("barcodeFormat"),
    invert: $("invert"),
    noBarcodeText: $("noBarcodeText"),
    caption: $("caption"),
    previewButton: $("previewButton"),
    dryRunButton: $("dryRunButton"),
    printButton: $("printButton"),
    previewImage: $("previewImage"),
    previewMeta: $("previewMeta"),
    message: $("message"),

    batchFile: $("batchFile"),
    batchText: $("batchText"),
    batchTemplate: $("batchTemplate"),
    batchStart: $("batchStart"),
    batchLimit: $("batchLimit"),
    batchDelay: $("batchDelay"),
    batchPreviewButton: $("batchPreviewButton"),
    batchDryRunButton: $("batchDryRunButton"),
    batchPrintButton: $("batchPrintButton"),
    batchMessage: $("batchMessage"),
  };

  function setMessage(text, type = "info") {
    elements.message.textContent = text;
    elements.message.className = `message ${type}`;
  }

  function clearMessage() {
    elements.message.textContent = "";
    elements.message.className = "message";
  }

  function setBatchMessage(text, type = "info") {
    elements.batchMessage.textContent = text;
    elements.batchMessage.className = `message ${type}`;
  }

  function currentPayload() {
    const base = {
      margin: Number(elements.margin.value),
      font_size: Number(elements.fontSize.value),
      align: elements.align.value,
      line_spacing: Number(elements.lineSpacing.value),
      threshold: Number(elements.threshold.value),
      invert: elements.invert.checked,
      qr_error: elements.qrError.value,
      barcode_format: elements.barcodeFormat.value,
      no_barcode_text: elements.noBarcodeText.checked,
      caption: elements.caption.value,
    };

    if (state.kind === "composite") {
      return {
        ...base,
        kind: "composite",
        text: elements.compositeText.value,
        qr: elements.compositeQr.value,
        barcode: elements.compositeBarcode.value,
        spacing: 4,
      };
    }

    return {
      ...base,
      kind: state.kind,
      text: elements.textInput.value,
      data: state.kind === "qr" ? elements.qrInput.value : elements.barcodeInput.value,
      image: state.imageData,
    };
  }

  async function api(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    return data;
  }

  function setBusy(busy) {
    state.busy = busy;
    elements.previewButton.disabled = busy;
    elements.printButton.disabled = busy;
    elements.dryRunButton.disabled = busy;
  }

  function setBatchBusy(busy) {
    state.batchBusy = busy;
    elements.batchPreviewButton.disabled = busy;
    elements.batchPrintButton.disabled = busy;
    elements.batchDryRunButton.disabled = busy;
  }

  function validatePayload(payload) {
    if (payload.kind === "text" && !payload.text.trim()) {
      throw new Error("请输入要打印的文字");
    }
    if (payload.kind === "qr" && !payload.data.trim()) {
      throw new Error("请输入二维码内容");
    }
    if (payload.kind === "barcode" && !payload.data.trim()) {
      throw new Error("请输入条形码内容");
    }
    if (payload.kind === "image" && !payload.image) {
      throw new Error("请先选择图片");
    }
    if (payload.kind === "composite" && !payload.text.trim() && !payload.qr.trim() && !payload.barcode.trim() && !payload.caption.trim()) {
      throw new Error("组合标签至少需要文字、二维码或条形码");
    }
  }

  async function refreshPreview() {
    try {
      const payload = currentPayload();
      validatePayload(payload);
      const data = await api("/api/preview", payload);
      elements.previewImage.src = data.image;
      elements.previewMeta.textContent = `预览尺寸：${data.width} × ${data.height} 点`;
      clearMessage();
    } catch (error) {
      setMessage(`预览失败：${error.message}`, "error");
    }
  }

  function schedulePreview() {
    clearTimeout(state.previewTimer);
    state.previewTimer = setTimeout(refreshPreview, 450);
  }

  function setKind(kind) {
    state.kind = kind;
    for (const tab of elements.tabs.querySelectorAll(".tab")) {
      tab.classList.toggle("active", tab.dataset.kind === kind);
    }
    for (const name of ["text", "qr", "barcode", "image", "composite"]) {
      $(`field-${name}`).classList.toggle("hidden", name !== kind);
    }
    if (kind === "image" && !state.imageData) {
      elements.previewImage.removeAttribute("src");
      elements.previewMeta.textContent = "请选择图片以生成预览";
    } else {
      schedulePreview();
    }
  }

  async function refreshStatus() {
    elements.status.textContent = "正在检查打印机…";
    elements.status.className = "status-badge checking";
    try {
      const response = await fetch("/api/status");
      const data = await response.json();
      if (!data.ok) throw new Error("状态接口返回失败");
      if (data.device_found) {
        elements.status.textContent = "打印机已连接";
        elements.status.className = "status-badge ok";
      } else {
        elements.status.textContent = "未找到 Mijia USB 打印机";
        elements.status.className = "status-badge error";
      }
      elements.siteUrl.textContent = data.url;
      elements.siteUrl.href = data.url;
      elements.devicePaths.textContent = data.device_paths.length
        ? `设备：${data.device_paths[0]}`
        : "设备：未发现 USB 打印接口";
      elements.siteQr.src = `/api/site-qr?url=${encodeURIComponent(data.url)}&t=${Date.now()}`;
    } catch (error) {
      elements.status.textContent = "状态检查失败";
      elements.status.className = "status-badge error";
      setMessage(`状态检查失败：${error.message}`, "error");
    }
  }

  async function printLabel(dryRun = false) {
    if (state.busy) return;
    try {
      const payload = currentPayload();
      validatePayload(payload);
      setBusy(true);
      clearMessage();
      setMessage(dryRun ? "正在生成打印帧…" : "正在通过 USB 发送打印任务…", "info");
      const data = await api(dryRun ? "/api/dry-run" : "/api/print", payload);
      if (dryRun) {
        setMessage(`测试成功：${data.frames} 帧，共 ${data.bytes} 字节。\n设备：${data.device_path}`, "ok");
      } else {
        setMessage(`打印任务已发送：${data.frames} 帧，共 ${data.bytes} 字节。\n请到打印机旁确认标签输出。`, "ok");
      }
    } catch (error) {
      setMessage(`${dryRun ? "测试" : "打印"}失败：${error.message}`, "error");
    } finally {
      setBusy(false);
    }
  }

  function parseBatchTemplate() {
    const text = elements.batchTemplate.value.trim();
    if (!text) throw new Error("请填写批量标签模板 JSON");
    const template = JSON.parse(text);
    if (!template || typeof template !== "object" || Array.isArray(template)) {
      throw new Error("批量模板必须是 JSON 对象");
    }
    return template;
  }

  function batchPayload() {
    const limitValue = elements.batchLimit.value.trim();
    return {
      file: state.batchFileData,
      filename: state.batchFilename,
      csv_text: state.batchFileData ? "" : elements.batchText.value,
      template: parseBatchTemplate(),
      start: Number(elements.batchStart.value || 1),
      limit: limitValue ? Number(limitValue) : null,
      label_delay: Number(elements.batchDelay.value || 1),
      margin: Number(elements.margin.value),
      font_size: Number(elements.fontSize.value),
      align: elements.align.value,
      line_spacing: Number(elements.lineSpacing.value),
      qr_error: elements.qrError.value,
      barcode_format: elements.barcodeFormat.value,
      no_barcode_text: elements.noBarcodeText.checked,
      spacing: 4,
      caption: elements.caption.value,
    };
  }

  function validateBatchPayload(payload) {
    if (!payload.file && !payload.csv_text.trim()) {
      throw new Error("请上传表格文件或粘贴 CSV/TSV 数据");
    }
    if (payload.file && !payload.filename) {
      throw new Error("缺少上传文件名");
    }
  }

  async function batchAction(kind) {
    if (state.batchBusy) return;
    try {
      const payload = batchPayload();
      validateBatchPayload(payload);
      setBatchBusy(true);
      setBatchMessage(
        kind === "preview" ? "正在生成第一张预览…" :
        kind === "dry-run" ? "正在计算批量打印帧…" : "正在批量发送打印任务…",
        "info"
      );
      const endpoint = kind === "preview" ? "/api/batch/preview" : kind === "dry-run" ? "/api/batch/dry-run" : "/api/batch/print";
      const data = await api(endpoint, payload);
      if (kind === "preview") {
        elements.previewImage.src = data.image;
        elements.previewMeta.textContent = `批量第 ${data.preview_row} 行预览：${data.width} × ${data.height} 点`;
        setBatchMessage(`表格共 ${data.total_rows} 行，本次可选 ${data.selected_rows} 行。`, "ok");
      } else if (kind === "dry-run") {
        setBatchMessage(
          `测试成功：共 ${data.selected_rows} 张标签，${data.total_frames} 帧，${data.total_bytes} 字节。`,
          "ok"
        );
      } else {
        setBatchMessage(
          `批量打印完成：共发送 ${data.printed_rows} 张标签。\n请到打印机旁确认输出。`,
          "ok"
        );
      }
    } catch (error) {
      setBatchMessage(`批量${kind === "preview" ? "预览" : kind === "dry-run" ? "测试" : "打印"}失败：${error.message}`, "error");
    } finally {
      setBatchBusy(false);
    }
  }

  elements.tabs.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) setKind(tab.dataset.kind);
  });

  elements.checkButton.addEventListener("click", refreshStatus);
  elements.previewButton.addEventListener("click", refreshPreview);
  elements.printButton.addEventListener("click", () => printLabel(false));
  elements.dryRunButton.addEventListener("click", () => printLabel(true));

  for (const id of [
    "textInput", "qrInput", "barcodeInput", "compositeText", "compositeQr",
    "compositeBarcode", "caption", "margin", "fontSize", "align", "lineSpacing",
    "threshold", "qrError", "barcodeFormat", "invert", "noBarcodeText",
  ]) {
    $(id).addEventListener("input", schedulePreview);
    $(id).addEventListener("change", schedulePreview);
  }

  elements.imageInput.addEventListener("change", () => {
    const file = elements.imageInput.files[0];
    if (!file) {
      state.imageData = null;
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.imageData = String(reader.result);
      refreshPreview();
    };
    reader.onerror = () => setMessage("读取图片失败", "error");
    reader.readAsDataURL(file);
  });

  elements.batchFile.addEventListener("change", () => {
    const file = elements.batchFile.files[0];
    if (!file) {
      state.batchFileData = null;
      state.batchFilename = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.batchFileData = String(reader.result);
      state.batchFilename = file.name;
      setBatchMessage(`已读取文件：${file.name}`, "info");
    };
    reader.onerror = () => setBatchMessage("读取表格文件失败", "error");
    reader.readAsDataURL(file);
  });

  elements.batchText.addEventListener("input", () => {
    if (elements.batchText.value.trim()) {
      state.batchFileData = null;
      state.batchFilename = "";
      elements.batchFile.value = "";
    }
  });

  elements.batchPreviewButton.addEventListener("click", () => batchAction("preview"));
  elements.batchDryRunButton.addEventListener("click", () => batchAction("dry-run"));
  elements.batchPrintButton.addEventListener("click", () => batchAction("print"));

  setKind("text");
  refreshStatus();
  refreshPreview();
})();
