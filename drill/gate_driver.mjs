#!/usr/bin/env node
/**
 * AgentTrace drill — gate driver.
 *
 * Connects one MCP client through AgentGate (demo upstream) with an
 * externally supplied trace ID (AGENTGATE_TRACE_ID), then calls the
 * poisoned tool (get_incident_notes) so the screening path fires.
 * Usage: node gate_driver.mjs <traceId> <stateDir>
 */
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [traceId, stateDir] = process.argv.slice(2);
if (!traceId || !stateDir) {
  console.error("usage: node gate_driver.mjs <traceId> <stateDir>");
  process.exit(2);
}
const gateRoot = join(import.meta.dirname, "..", "..", "agent-gate");
// This driver lives outside the agent-gate package, so resolve the MCP SDK
// from agent-gate's own node_modules by absolute path.
const sdkModule = (p) =>
  pathToFileURL(join(gateRoot, "node_modules", "@modelcontextprotocol", "sdk", "dist", "esm", p)).href;
const { Client } = await import(sdkModule("client/index.js"));
const { StdioClientTransport } = await import(sdkModule("client/stdio.js"));

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [
    join(gateRoot, "dist/src/index.js"),
    "--upstream",
    process.execPath,
    join(gateRoot, "demo-upstream/server.mjs"),
  ],
  env: {
    ...process.env,
    AGENTGATE_TOKEN: "demo-agent-token",
    AGENTGATE_POLICY: join(gateRoot, "policy.json"),
    AGENTGATE_STATE_DIR: stateDir,
    AGENTGATE_TRACE_ID: traceId,
  },
  stderr: "inherit",
});
const client = new Client({ name: "agent-trace-drill", version: "0.1.0" }, { capabilities: {} });
await client.connect(transport);
const { tools } = await client.listTools();
console.log(`tools visible: ${tools.map((t) => t.name).join(", ")}`);
const notes = await client.callTool({ name: "get_incident_notes", arguments: {} });
const text = notes.content.filter((c) => c.type === "text").map((c) => c.text).join("\n");
console.log(`poisoned result screened: warning banner = ${text.includes("AGENTGATE SCREENING WARNING")}, ssn leaked = ${text.includes("123-45-6789")}`);
await client.close();
console.log("gate driver done");
