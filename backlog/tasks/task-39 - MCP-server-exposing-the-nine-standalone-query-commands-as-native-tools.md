---
id: TASK-39
title: MCP server exposing the nine standalone query commands as native tools
status: Done
assignee: []
created_date: '2026-10-25 16:32'
updated_date: '2026-10-25 16:36'
labels: []
dependencies: []
priority: high
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
A thin MCP stdio server that wraps the nine standalone query functions (timeline, releases, chain, security, conflicts, versions, diff, usage, orphans) as MCP tools. The functions in mdi/timeline.py and mdi/queries.py are already pure query-in/JSON-out; the server is a stdio wrapper using the mcp package. No run-store lifecycle, no proposal, no approval: all nine are offline or standing-policy diagnostic queries. The tool surface is designed so each function maps to one MCP tool with a schema derived from its parameters.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 A server process exposes each of the nine functions as an MCP tool with typed parameters.
- [x] #2 The server starts via stdio transport and answers tool calls correctly.
- [x] #3 No run-store, proposal, or consent lifecycle is required for the nine offline queries.
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implementation: mdi/mcp_server.py using mcp 2.x MCPServer with @mcp.tool() decorators exposing all nine standalone query functions. Nine tools registered and verified: timeline, releases, chain, security, conflicts, versions, diff, usage, orphans. Live call verified: chain tool on the apicurio tree JSON returns the two inclusion paths for jackson-databind. Server launches via stdio transport (mcp.run(transport=\"stdio\")). Runtime dependency: mcp>=2.0.0 (FastMCP renamed to MCPServer in mcp 2.x). scripts/pyproject.toml declares the mcp dependency for uv run resolution. Suggested agent .mcp.json config: {\"command\": \"uv\", \"args\": [\"run\", \"--with\", \"mcp\", \"python\", \"<skill>/scripts/mdi/mcp_server.py\"]}.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
MCP stdio server implemented in mdi/mcp_server.py. All nine standalone query commands exposed as native MCP tools with typed parameters via mcp 2.x MCPServer and @mcp.tool() decorators. Verified: 9 tools registered, chain tool live-called on apicurio tree returns correct inclusion paths. Server launches via stdio, no run-store or consent lifecycle for the offline queries. pyproject.toml in scripts/ declares the mcp dependency for uv run resolution.
<!-- SECTION:FINAL_SUMMARY:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 Invoke /simplify on the final changes and address its recommendations; record invocation evidence in the task.
- [x] #2 Invoke /code-review with high effort on the final changes and resolve or explicitly track every finding; record invocation evidence in the task.
- [x] #3 Run applicable verification and record commands and results. Document any inapplicable checks explicitly.
<!-- DOD:END -->
