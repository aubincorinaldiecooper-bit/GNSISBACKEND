import { ActionProblem, required, type ActionTool, type PreparedAction } from "../actions.js";
import type { BrowserHubServer } from "../../host/browserHub.js";

/**
 * Give one complete user browser task to the connected GNSIS Browser extension.
 * The extension keeps its own persistent Panoptic -> Laya -> Actuator loop.
 */
export class BrowserAgentTool implements ActionTool {
  readonly name = "browser_agent";
  readonly platforms = ["darwin"] as const;

  constructor(private readonly hub: BrowserHubServer) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const task = required(args, "task", "what to do in the browser");
    return {
      tool: this.name,
      action: "run",
      effect: "input",
      summary: `Run this browser task: ${task.slice(0, 160)}`,
      scope: [],
      run: async () => {
        if (!this.hub.connected) {
          throw new ActionProblem(
            "not_found",
            `GNSIS Browser is not connected. Open ${this.hub.extensionUrl} in Chrome after installing the extension.`,
          );
        }
        const result = await this.hub.execute(task);
        if (!result.success) {
          throw new ActionProblem("failed", result.data || "The browser task did not complete.");
        }
        return {
          verified: "screen",
          message: result.data || "Browser task completed.",
          detail: { browser_agent: "gnsis-browser" },
        };
      },
    };
  }
}
