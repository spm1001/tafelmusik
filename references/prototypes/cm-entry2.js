import {EditorView, basicSetup} from "codemirror";
import {markdown} from "@codemirror/lang-markdown";
import {marked} from "marked";

const SAMPLE = `# Quarterly Review: MIT Q1 2026

## Executive Summary

The Measurement Innovation Team delivered **three major initiatives** this quarter, with *mixed results* across the portfolio.

## Key Metrics

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| Data pipeline uptime | 99.5% | 99.8% | On track |
| Report turnaround | 48hrs | 36hrs | Ahead |
| Stakeholder NPS | 40 | 32 | Behind |

## Highlights

1. **Pipeline modernisation** — migrated 12 legacy ETL jobs to the new framework.
2. **Self-serve dashboards** — launched the new dashboard builder. Early adoption is strong.
3. **Cross-platform measurement** — proof of concept delivered.

## Risks and Concerns

- Stakeholder NPS dropped from 38 to 32. Root cause: communication gaps.
- BigQuery cost model changed mid-quarter. 15% increase without budget adjustment.
- One team member on extended leave; coverage is adequate but stretched.

> "Measure what matters, not what's easy." — Internal motto

### Must Do

- Complete cross-platform methodology peer review
- Address stakeholder communication gaps

\`\`\`python
pipeline = Pipeline(
    name="cross_platform_ingest",
    schedule="0 */4 * * *",
    sources=["linear_tv", "bvod", "social"],
)
\`\`\`

---

*Document prepared by MIT. Last updated 2026-03-28.*`;

function init() {
  const updatePreview = EditorView.updateListener.of((update) => {
    if (update.docChanged) {
      const preview = document.getElementById('preview-pane');
      if (preview && preview.style.display !== 'none') {
        preview.innerHTML = marked.parse(update.state.doc.toString());
      }
    }
  });

  const view = new EditorView({
    doc: SAMPLE,
    extensions: [basicSetup, markdown(), updatePreview, EditorView.lineWrapping],
    parent: document.getElementById('editor-pane'),
  });

  window.setMode = (mode) => {
    const editorPane = document.getElementById('editor-pane');
    const previewPane = document.getElementById('preview-pane');
    document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
    if (mode === 'edit') {
      editorPane.style.display = 'block';
      previewPane.style.display = 'none';
      document.getElementById('btn-edit').classList.add('active');
    } else if (mode === 'split') {
      editorPane.style.display = 'block';
      previewPane.style.display = 'block';
      previewPane.innerHTML = marked.parse(view.state.doc.toString());
      document.getElementById('btn-split').classList.add('active');
    } else {
      editorPane.style.display = 'none';
      previewPane.style.display = 'block';
      previewPane.innerHTML = marked.parse(view.state.doc.toString());
      document.getElementById('btn-preview').classList.add('active');
    }
  };
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
