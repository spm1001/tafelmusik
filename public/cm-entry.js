import {EditorView, basicSetup, minimalSetup} from "codemirror";
import {keymap, Decoration, ViewPlugin} from "@codemirror/view";
import {RangeSetBuilder, Annotation} from "@codemirror/state";
import {markdown} from "@codemirror/lang-markdown";
import {HighlightStyle, syntaxHighlighting} from "@codemirror/language";
import {tags} from "@lezer/highlight";
import * as Y from "yjs";
import {yCollab} from "y-codemirror.next";
import {WebsocketProvider} from "y-websocket";

const headingStyle = HighlightStyle.define([
  {tag: tags.heading1, fontWeight: "bold", fontSize: "1.6em", textDecoration: "none"},
  {tag: tags.heading2, fontWeight: "bold", fontSize: "1.4em", textDecoration: "none"},
  {tag: tags.heading3, fontWeight: "bold", fontSize: "1.2em", textDecoration: "none"},
  {tag: tags.heading4, fontWeight: "bold", fontSize: "1.1em", textDecoration: "none"},
  {tag: tags.heading5, fontWeight: "bold", textDecoration: "none"},
  {tag: tags.heading6, fontWeight: "bold", textDecoration: "none"},
]);

const commentMark = Decoration.mark({class: "cm-comment-highlight"});

// Annotation to mark programmatic selections (card clicks) — not user intent to comment
const programmatic = Annotation.define();

function init() {
  const room = window.location.pathname.replace(/^\/+/, "") || "default";
  document.getElementById("room-name").textContent = room;

  const ydoc = new Y.Doc();
  const ytext = ydoc.getText("content");
  const comments = ydoc.getMap("comments");

  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProto}//${window.location.host}/_ws`;
  const provider = new WebsocketProvider(wsUrl, room, ydoc);

  provider.on("status", ({status}) => {
    const el = document.getElementById("connection-status");
    el.textContent = status === "connected" ? "connected" : status;
    el.className = "status " + status;
  });

  // --- UI state machine ---
  //
  // Two modes:
  //   "document"   — editing the document, compose card hidden
  //   "commenting"  — compose card visible, writing a comment
  //
  // Transitions:
  //   document  → commenting  : user selects text
  //   commenting → document   : submit, cancel, or click away (empty compose)
  //   commenting → commenting : user selects different text (compose moves)

  let mode = "document";
  let view = null;
  let commentRanges = [];
  let orphanedComments = [];
  let activeCommentId = null;
  let capturedSelection = null; // {from, to, text} — set in "commenting" mode

  function enterCommenting(sel) {
    capturedSelection = sel;
    mode = "commenting";
    composeQuoteEl.textContent = sel.text;
    composeCard.style.display = "block";
    positionComposeCard();
    requestAnimationFrame(() => composeView.focus());
  }

  function enterDocument() {
    mode = "document";
    capturedSelection = null;
    composeView.dispatch({changes: {from: 0, to: composeView.state.doc.length}});
    composeCard.style.display = "none";
    if (composeCard.parentNode) composeCard.remove();
    renderComments();
  }

  // --- Resolve comment ranges ---

  function resolveRanges() {
    const ranges = [];
    const orphans = [];
    const docText = view ? view.state.doc.toString() : "";

    comments.forEach((comment, id) => {
      if (!(comment instanceof Y.Map)) return;
      if (comment.get("resolved")) return;

      const quote = comment.get("quote");
      if (!quote) return;

      if (comment.get("orphaned")) {
        orphans.push({id, quote, body: comment.get("body"), author: comment.get("author"), created: comment.get("created") || ""});
        return;
      }

      try {
        let from = -1, to = -1;

        const startJson = comment.get("anchorStart");
        const endJson = comment.get("anchorEnd");
        if (startJson && endJson) {
          const startPos = Y.createAbsolutePositionFromRelativePosition(
            Y.createRelativePositionFromJSON(JSON.parse(startJson)), ydoc);
          const endPos = Y.createAbsolutePositionFromRelativePosition(
            Y.createRelativePositionFromJSON(JSON.parse(endJson)), ydoc);
          if (startPos && endPos && startPos.index < endPos.index) {
            from = startPos.index;
            to = endPos.index;
          }
        }

        if (from === -1) {
          const anchorJson = comment.get("anchor");
          if (!anchorJson) return;
          const relPos = Y.createRelativePositionFromJSON(JSON.parse(anchorJson));
          const absPos = Y.createAbsolutePositionFromRelativePosition(relPos, ydoc);
          if (!absPos) return;

          const idx = absPos.index;
          const searchFrom = Math.max(0, idx - quote.length);
          const searchTo = Math.min(docText.length, idx + quote.length * 2);
          const region = docText.slice(searchFrom, searchTo);
          const match = region.indexOf(quote);
          if (match === -1) return;
          from = searchFrom + match;
          to = from + quote.length;
        }

        if (from >= 0 && to > from && to <= docText.length) {
          ranges.push({from, to, id, quote, body: comment.get("body"), author: comment.get("author"), created: comment.get("created") || ""});
        }
      } catch (e) {}
    });

    // Comments with overlapping ranges are "same conversation" → sort by creation time.
    // Non-overlapping comments sort by document position.
    ranges.sort((a, b) => {
      const overlaps = a.from < b.to && b.from < a.to;
      if (overlaps) return a.created.localeCompare(b.created);
      return a.from - b.from;
    });
    commentRanges = ranges;
    orphanedComments = orphans;
  }

  // --- Comment decorations ---

  const commentPlugin = ViewPlugin.define((v) => {
    let alive = true;

    const plugin = {
      decorations: Decoration.none,

      rebuild() {
        resolveRanges();
        // Decorations must be added in strict from-position order
        const decoRanges = [...commentRanges].sort((a, b) => a.from - b.from || a.to - b.to);
        const builder = new RangeSetBuilder();
        for (const r of decoRanges) {
          builder.add(r.from, r.to, commentMark);
        }
        this.decorations = builder.finish();
      },

      update(update) {
        if (update.docChanged) {
          this.rebuild();
          renderComments();
        }
        if (update.selectionSet && view) {
          // Ignore programmatic selections (card clicks)
          if (update.transactions.some((t) => t.annotation(programmatic))) return;

          const {from, to} = update.state.selection.main;
          if (from !== to) {
            // User selected text → enter or update commenting
            const text = update.state.doc.sliceString(from, to);
            enterCommenting({from, to, text});
          } else if (mode === "commenting") {
            // Selection collapsed → return to document if compose is empty
            if (composeView.state.doc.length === 0) enterDocument();
          }
        }
      },

      destroy() {
        alive = false;
        comments.unobserveDeep(observer);
      },
    };

    const observer = () => {
      if (!alive) return;
      plugin.rebuild();
      const wasCommenting = mode === "commenting";
      renderComments();
      if (wasCommenting) requestAnimationFrame(() => composeView.focus());
      try { v.dispatch({}); } catch (e) {}
    };
    comments.observeDeep(observer);

    return plugin;
  }, {
    decorations: (v) => v.decorations,
  });

  // --- Compose card ---

  const composeCard = document.createElement("div");
  composeCard.className = "compose-card";
  composeCard.style.display = "none";

  const composeQuoteEl = document.createElement("div");
  composeQuoteEl.className = "comment-quote";

  const composeEditorWrap = document.createElement("div");
  composeEditorWrap.className = "compose-editor";

  const composeHint = document.createElement("div");
  composeHint.className = "compose-hint";
  composeHint.textContent = "\u2318\u21A9 or Shift\u21A9 to comment \u00B7 Esc to cancel";

  composeCard.append(composeQuoteEl, composeEditorWrap, composeHint);

  function submitComment() {
    const body = composeView.state.doc.toString().trim();
    if (!body || !capturedSelection) return false;

    const {from, to, text} = capturedSelection;

    ydoc.transact(() => {
      const comment = new Y.Map();
      const id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      comments.set(id, comment);
      const startRelPos = Y.createRelativePositionFromTypeIndex(ytext, from);
      const endRelPos = Y.createRelativePositionFromTypeIndex(ytext, to);
      comment.set("anchorStart", JSON.stringify(Y.relativePositionToJSON(startRelPos)));
      comment.set("anchorEnd", JSON.stringify(Y.relativePositionToJSON(endRelPos)));
      comment.set("anchor", JSON.stringify(Y.relativePositionToJSON(startRelPos)));
      comment.set("quote", text);
      comment.set("author", "sameer");
      comment.set("body", body);
      comment.set("resolved", false);
      comment.set("created", new Date().toISOString());
    });

    enterDocument();
    if (view) view.focus();
    return true;
  }

  function cancelCompose() {
    enterDocument();
    if (view) view.focus();
    return true;
  }

  const composeView = new EditorView({
    extensions: [
      keymap.of([
        {key: "Mod-Enter", run: () => submitComment()},
        {key: "Shift-Enter", run: () => submitComment()},
        {key: "Escape", run: () => cancelCompose()},
      ]),
      minimalSetup,
      markdown(),
      EditorView.lineWrapping,
    ],
    parent: composeEditorWrap,
  });

  // --- Compose positioning ---

  function positionComposeCard() {
    if (!capturedSelection) return;

    const list = document.getElementById("comments-list");
    const insertPos = capturedSelection.from;

    const cards = [...list.querySelectorAll(".comment-card")];
    let insertBefore = null;
    for (const card of cards) {
      const cardFrom = parseInt(card.dataset.from, 10);
      if (!isNaN(cardFrom) && cardFrom > insertPos) {
        insertBefore = card;
        break;
      }
    }

    if (composeCard.parentNode) composeCard.remove();
    list.insertBefore(composeCard, insertBefore);
  }

  // --- Comments pane ---

  const commentsList = document.getElementById("comments-list");

  function renderComments() {
    const ranges = commentRanges;

    if (composeCard.parentNode) composeCard.remove();
    commentsList.innerHTML = "";

    if (ranges.length === 0 && mode === "document") {
      const empty = document.createElement("div");
      empty.className = "comments-empty";
      empty.textContent = "Select text to comment";
      commentsList.appendChild(empty);
    }

    for (const range of ranges) {
      const card = document.createElement("div");
      card.className = "comment-card author-" + (range.author || "unknown") + (range.id === activeCommentId ? " active" : "");
      card.dataset.from = range.from;

      const author = document.createElement("div");
      author.className = "comment-author";
      author.textContent = range.author;

      const quote = document.createElement("div");
      quote.className = "comment-quote";
      quote.textContent = range.quote;

      const body = document.createElement("div");
      body.className = "comment-body";
      body.textContent = range.body;

      const resolve = document.createElement("button");
      resolve.className = "comment-resolve";
      resolve.textContent = "Resolve";
      resolve.addEventListener("click", (e) => {
        e.stopPropagation();
        const comment = comments.get(range.id);
        if (comment) comment.set("resolved", true);
      });

      card.append(author, quote, body, resolve);

      card.addEventListener("click", () => {
        activeCommentId = range.id;
        if (mode === "commenting") enterDocument();
        renderComments();
        if (view) {
          view.dispatch({
            selection: {anchor: range.from, head: range.to},
            effects: EditorView.scrollIntoView(range.from, {y: "center"}),
            annotations: programmatic.of(true),
          });
          view.focus();
        }
      });

      commentsList.appendChild(card);
    }

    // Orphaned comments — greyed out at the bottom
    for (const orphan of orphanedComments) {
      const card = document.createElement("div");
      card.className = "comment-card orphaned";

      const author = document.createElement("div");
      author.className = "comment-author";
      author.textContent = orphan.author + " \u00B7 orphaned";

      const quote = document.createElement("div");
      quote.className = "comment-quote";
      quote.textContent = orphan.quote;

      const body = document.createElement("div");
      body.className = "comment-body";
      body.textContent = orphan.body;

      const resolve = document.createElement("button");
      resolve.className = "comment-resolve";
      resolve.textContent = "Resolve";
      resolve.addEventListener("click", (e) => {
        e.stopPropagation();
        const comment = comments.get(orphan.id);
        if (comment) comment.set("resolved", true);
      });

      card.append(author, quote, body, resolve);
      commentsList.appendChild(card);
    }

    // Re-insert compose card if commenting
    if (mode === "commenting") {
      positionComposeCard();
    }
  }

  // --- Editor ---

  view = new EditorView({
    extensions: [
      basicSetup,
      markdown(),
      syntaxHighlighting(headingStyle),
      yCollab(ytext),
      commentPlugin,
      EditorView.lineWrapping,
    ],
    parent: document.getElementById("editor-pane"),
  });

  provider.on("sync", (synced) => {
    if (synced) {
      resolveRanges();
      renderComments();
    }
  });

  // --- File browser ---

  const filesPane = document.getElementById("files-pane");
  const filesTree = document.getElementById("files-tree");
  const filesToggle = document.getElementById("files-toggle");
  const filesClose = document.getElementById("files-close");
  const filesSearch = document.getElementById("files-search");

  filesToggle.addEventListener("click", () => {
    filesPane.classList.toggle("collapsed");
  });
  filesClose.addEventListener("click", () => {
    filesPane.classList.add("collapsed");
  });

  // Resize handle
  const filesResize = document.getElementById("files-resize");
  filesResize.addEventListener("mousedown", (e) => {
    e.preventDefault();
    filesResize.classList.add("dragging");
    const onMove = (e) => {
      const w = Math.max(160, Math.min(e.clientX, window.innerWidth / 2));
      filesPane.style.width = w + "px";
    };
    const onUp = () => {
      filesResize.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // Recently accessed — stored in localStorage
  function getRecents() {
    try { return JSON.parse(localStorage.getItem("tfm-recents") || "[]"); } catch { return []; }
  }
  function addRecent(name) {
    let recents = getRecents().filter((r) => r !== name);
    recents.unshift(name);
    if (recents.length > 20) recents = recents.slice(0, 20);
    localStorage.setItem("tfm-recents", JSON.stringify(recents));
  }

  if (room !== "default") addRecent(room);

  let allRooms = [];

  function makeFileItem(r) {
    const item = document.createElement("div");
    item.className = "file-item" + (r.name === room ? " current" : "");

    const dot = document.createElement("span");
    dot.className = "dot " + (r.active ? "active" : "inactive");
    item.appendChild(dot);

    const label = document.createElement("span");
    label.textContent = r.name;
    item.appendChild(label);

    item.addEventListener("click", () => {
      addRecent(r.name);
      window.location.pathname = "/" + r.name;
    });
    return item;
  }

  function renderFileList() {
    const query = filesSearch.value.trim().toLowerCase();
    filesTree.innerHTML = "";

    if (query) {
      // Search mode — filter all rooms, show up to 30 matches
      const matches = allRooms.filter((r) => r.name.toLowerCase().includes(query)).slice(0, 30);
      if (matches.length) {
        for (const r of matches) filesTree.appendChild(makeFileItem(r));
      } else {
        const hint = document.createElement("div");
        hint.className = "search-hint";
        hint.textContent = "No matches. Enter to create.";
        filesTree.appendChild(hint);
      }
      return;
    }

    // Default: active rooms + recents
    const recents = getRecents();
    const active = allRooms.filter((r) => r.active);
    const roomMap = new Map(allRooms.map((r) => [r.name, r]));

    if (active.length) {
      const section = document.createElement("div");
      section.className = "files-section";
      const label = document.createElement("div");
      label.className = "files-section-label";
      label.textContent = "Active";
      section.appendChild(label);
      for (const r of active) section.appendChild(makeFileItem(r));
      filesTree.appendChild(section);
    }

    const recentRooms = recents
      .filter((name) => roomMap.has(name) && !roomMap.get(name).active)
      .map((name) => roomMap.get(name));
    if (recentRooms.length) {
      const section = document.createElement("div");
      section.className = "files-section";
      const label = document.createElement("div");
      label.className = "files-section-label";
      label.textContent = "Recent";
      section.appendChild(label);
      for (const r of recentRooms) section.appendChild(makeFileItem(r));
      filesTree.appendChild(section);
    }

    if (!active.length && !recentRooms.length) {
      const hint = document.createElement("div");
      hint.className = "search-hint";
      hint.textContent = "Type to search documents";
      filesTree.appendChild(hint);
    }
  }

  filesSearch.addEventListener("input", renderFileList);
  filesSearch.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const path = filesSearch.value.trim().replace(/\.md$/, "");
      if (path) {
        addRecent(path);
        window.location.pathname = "/" + path;
      }
    }
  });

  async function refreshFiles() {
    try {
      const res = await fetch("/api/rooms");
      const data = await res.json();
      allRooms = data.rooms || [];
      renderFileList();
    } catch (e) {}
  }

  refreshFiles();
  setInterval(refreshFiles, 10000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
