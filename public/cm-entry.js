import {EditorView, basicSetup} from "codemirror";
import {markdown} from "@codemirror/lang-markdown";
import {marked} from "marked";
import * as Y from "yjs";
import {yCollab} from "y-codemirror.next";
import {WebsocketProvider} from "y-websocket";

function init() {
  const room = new URLSearchParams(window.location.search).get("room") || "default";
  document.getElementById("room-name").textContent = room;

  const ydoc = new Y.Doc();
  const ytext = ydoc.getText("content");

  // Connect to the ASGI server's WebSocket endpoint
  const wsUrl = `ws://${window.location.host}`;
  const provider = new WebsocketProvider(wsUrl, room, ydoc);

  provider.on("status", ({status}) => {
    const el = document.getElementById("connection-status");
    el.textContent = status === "connected" ? "connected" : status;
    el.className = "status " + status;
  });

  const updatePreview = EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      const preview = document.getElementById("preview-pane");
      if (preview && preview.style.display !== "none") {
        preview.innerHTML = marked.parse(update.state.doc.toString());
      }
    }
  });

  const view = new EditorView({
    extensions: [
      basicSetup,
      markdown(),
      yCollab(ytext),
      updatePreview,
      EditorView.lineWrapping,
    ],
    parent: document.getElementById("editor-pane"),
  });

  // Render preview on initial load once synced
  provider.on("sync", (synced) => {
    if (synced) {
      const preview = document.getElementById("preview-pane");
      if (preview && preview.style.display !== "none") {
        preview.innerHTML = marked.parse(ytext.toString());
      }
    }
  });

  window.setMode = (mode) => {
    const editorPane = document.getElementById("editor-pane");
    const previewPane = document.getElementById("preview-pane");
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    if (mode === "edit") {
      editorPane.style.display = "block";
      previewPane.style.display = "none";
      document.getElementById("btn-edit").classList.add("active");
    } else if (mode === "split") {
      editorPane.style.display = "block";
      previewPane.style.display = "block";
      previewPane.innerHTML = marked.parse(view.state.doc.toString());
      document.getElementById("btn-split").classList.add("active");
    } else {
      editorPane.style.display = "none";
      previewPane.style.display = "block";
      previewPane.innerHTML = marked.parse(view.state.doc.toString());
      document.getElementById("btn-preview").classList.add("active");
    }
  };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
