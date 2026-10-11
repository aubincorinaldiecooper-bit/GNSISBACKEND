import { ActionProblem, required, str, type ActionTool, type PreparedAction } from "../actions.js";
import { CuaToolError, type CuaControl } from "../cua/driver.js";

export class ClipboardTool implements ActionTool {
  readonly name = "clipboard";
  readonly platforms = ["darwin"] as const;

  constructor(private readonly cua: CuaControl) {}

  async prepare(args: Record<string, unknown>): Promise<PreparedAction> {
    const action = str(args, "action");
    if (action === "read") {
      const includeText = args.include_text === true;
      return {
        tool: this.name,
        action,
        effect: "read",
        summary: includeText ? "Read the clipboard text" : "See which clipboard types are available",
        scope: [],
        run: async () => {
          const result = await this.call("clipboard_read", { include_text: includeText });
          return { verified: "app", message: "Read the clipboard through Cua.", detail: { clipboard: result.structured } };
        },
      };
    }
    if (action === "write") {
      const text = typeof args.text === "string" ? args.text : undefined;
      const filePath = str(args, "file_path");
      const imagePath = str(args, "image_path");
      const given = [text !== undefined, !!filePath, !!imagePath].filter(Boolean).length;
      if (given !== 1) throw new ActionProblem("failed", "Clipboard write needs exactly one of text, file_path, or image_path.");
      const named = text ?? filePath ?? imagePath!;
      return {
        tool: this.name,
        action,
        effect: "input",
        summary: text !== undefined ? "Put the requested text on the clipboard" : `Put ${filePath ?? imagePath} on the clipboard`,
        scope: [{ value: named, source: "named" }],
        run: async () => {
          const result = await this.call("clipboard_write", {
            ...(text !== undefined ? { text } : {}),
            ...(filePath ? { file_path: filePath } : {}),
            ...(imagePath ? { image_path: imagePath } : {}),
          });
          return { verified: "app", message: "Updated the clipboard through Cua.", detail: { clipboard: result.structured } };
        },
      };
    }
    throw new ActionProblem("unsupported", `clipboard cannot ${String(action)}.`);
  }

  private async call(tool: string, args: Record<string, unknown>) {
    try { return await this.cua.call(tool, args); }
    catch (error) {
      if (error instanceof CuaToolError) throw new ActionProblem("failed", error.message, { provider: "cua", code: error.code });
      throw error;
    }
  }
}
