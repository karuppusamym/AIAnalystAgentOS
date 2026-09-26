# OKF test fixtures (P4-K02, P4-K09)

| File | What it is | How it was made |
|---|---|---|
| `atlas-sample.zip` | A real Atlas OKF v0.2 bundle archive: `atlas-manifest.json` beside a `bundle/` root, 11 documents (source, schema, table, view, three routines, package, business concept, indexes). | Rendered on 2026-09-25 by Atlas's own exporter (`aida.okf_export.export_okf_bundle` + `bundle_archive_bytes`, AIDataAnalyst checkout of that day, profile `atlas-okf-export/4`) from the hand-built snapshot in Atlas's `tests/test_okf_export.py::_snapshot`. Bytes unchanged. sha256 `4e9bcee2869160a5b4d7a5b3f014abdebae8db67a62a3e908ed39df18496fb2f`. |
| `atlas-mcp-get-knowledge-context.mock.json` | **MOCK.** A `tools/call` result of Atlas's `atlas__get_knowledge_context`, as the MCP double in the tests serves it. | The selection (documents, sections, Markdown) was computed by Atlas's own `aida.okf_context.select_context` / `render_markdown` over the sample bundle above, then wrapped the way `aida.mcp_server._handle_native_knowledge_tool_call` wraps it. Publication ids and the egress screening version are synthetic. **Not captured from a live Atlas instance**: a test passing against it proves AnalystOS's client and parser, not compatibility with a running Atlas deployment (spec v1 §62: mocks do not certify). |

Both files are data. Nothing in them is executed.
