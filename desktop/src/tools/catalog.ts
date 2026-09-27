/**
 * The desktop's half of the host-tool contract.
 *
 * The runtime shows the model the schemas in this very file
 * (runtime/configs/gnsis-host-tools.json, `duplex.host_tools_path`); the
 * desktop bundles the same file, so the arguments the model was told about
 * are the arguments checked here. The desktop offers a tool by name and this
 * version when it connects, and the runtime refuses the offer outright when
 * the versions differ.
 */
import catalog from "../../../runtime/configs/gnsis-host-tools.json";

export type JsonSchema = {
  type?: string | string[];
  enum?: unknown[];
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean;
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  description?: string;
};

export interface HostToolSchema {
  name: string;
  description: string;
  parameters: JsonSchema;
}

export const HOST_TOOLS_VERSION: string = catalog.version;
export const HOST_TOOL_SCHEMAS: readonly HostToolSchema[] = catalog.tools as unknown as HostToolSchema[];

export function hostToolSchema(name: string): HostToolSchema | undefined {
  return HOST_TOOL_SCHEMAS.find((tool) => tool.name === name);
}

/**
 * Check arguments against the catalog schema, the same subset the model core
 * checks before a call leaves the runtime. Returns a sentence, or null.
 *
 * The runtime already validated the call, so a failure here means the two
 * sides disagree about the schema — which is exactly when nothing should run.
 */
export function checkArguments(name: string, args: unknown): string | null {
  const schema = hostToolSchema(name);
  if (!schema) return `${name} is not a tool this desktop knows`;
  return checkValue(args, schema.parameters, name);
}

function checkValue(value: unknown, schema: JsonSchema, path: string): string | null {
  const types = schema.type === undefined ? [] : Array.isArray(schema.type) ? schema.type : [schema.type];
  if (types.length && !types.some((t) => matchesType(value, t))) {
    return `${path} must be ${types.join(" or ")}`;
  }
  if (schema.enum && !schema.enum.includes(value)) return `${path} is not one of ${schema.enum.join(", ")}`;
  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < schema.minLength) return `${path} is too short`;
    if (schema.maxLength !== undefined && value.length > schema.maxLength) return `${path} is too long`;
  }
  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < schema.minimum) return `${path} is below ${schema.minimum}`;
    if (schema.maximum !== undefined && value > schema.maximum) return `${path} is above ${schema.maximum}`;
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const key of schema.required ?? []) {
      if (!(key in record)) return `${path}.${key} is required`;
    }
    const properties = schema.properties ?? {};
    for (const [key, child] of Object.entries(record)) {
      const childSchema = properties[key];
      if (!childSchema) {
        if (schema.additionalProperties === false) return `${path} has an unexpected field ${key}`;
        continue;
      }
      const problem = checkValue(child, childSchema, `${path}.${key}`);
      if (problem) return problem;
    }
  }
  return null;
}

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      return false;
  }
}
