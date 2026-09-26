// Stand-in content for the parts of the product that have no backend yet:
// agents, their panels, approvals, the canned replies to typed messages.
// Live voice is NOT here — it is real, fed by the host. Everything in this file
// is replaced by real agent output later; the shapes stay.

export type Role = "user" | "agent" | "system";

export interface Turn {
  role: Role;
  text: string;
  /** the person said it rather than typed it */
  spoken?: boolean;
  /** how long a spoken turn lasted, when its words are not available */
  spokenMs?: number;
  /** characters of an agent turn revealed so far (streaming) */
  stream?: number;
  /** live-voice turn still being spoken */
  speaking?: boolean;
}

export type PanelKind = "browser" | "list" | "email" | "watch" | "agents";

export interface ListSpec {
  title: string;
  style: "check" | "num" | "chev";
  rows: string[];
  progressive?: boolean;
}

export interface Conv {
  id: string;
  title: string;
  /** face seed; GNSIS uses the device's public ID */
  faceKey?: string;
  kind: "home" | "chat" | "agent";
  req: string;
  spoken?: boolean;
  ans: string;
  stream: number;
  turns: Turn[];
  panel: PanelKind;
  list?: ListSpec;
  doneText: string;
  shortReply: string;
  needs?: boolean;
  approval?: "pending" | "answered";
  answeredLabel?: string;
  follow?: string;
  followStream?: number;
  drafting?: boolean;
  draftStream?: number;
  forever?: boolean;
  stopped?: boolean;
  unread?: boolean;
  archived?: boolean;
  readAt?: number;
  bornT?: number;
  agentT?: number;
  agentStream?: number;
  stepsOpen?: boolean;
}

export const TICK_MS = 70;
export const STEP_TICKS = 15;

export const QUESTION = "What’s a good way to start my morning with more focus?";
export const FOLLOW_PHRASE = "Can you give me the short version?";

export const MORNING_ANSWER =
  "Start small, and keep it the same every day.\n\nBefore you look at your phone, drink a glass of water and get a few minutes of daylight. It tells your body the day has started.\n\nThen pick the one task that matters most and give it your first focused block, with notifications off.\n\nLeave email and messages until that block is done, so other people’s priorities don’t set your morning.";

export const ROOF_ANSWER =
  "Ask what the price includes, and what could change it once the old roof is off.\n\nCheck that they’re licensed and insured, and ask to see proof.\n\nGet the warranty in writing, for both the materials and the labor.";

export const ROOF_DRAFT =
  "Subject: Questions about the roof estimate\n\nHi,\n\nThanks for sending the estimate. Before I decide, could you help me with a few things?\n\n1. What does the price include, and what could change once the old roof is off?\n2. Could you send proof that you’re licensed and insured?\n3. What warranty do you give on materials and labor, in writing?\n\nThanks,\n[Your name]";

export const AGENT_STEPS = [
  { label: "Opening recipes.example", detail: "" },
  { label: "Searching for cacio e pepe", detail: "" },
  { label: "Choosing a recipe for two", detail: "20 minutes" },
  { label: "Reading the ingredients", detail: "4 items" },
];
export const AGENT_TOTAL = AGENT_STEPS.length * STEP_TICKS;
export const AGENT_ANSWER =
  "Done. The recipe serves two and takes about 20 minutes. You need spaghetti, Pecorino Romano, black peppercorns and salt. It’s open in my panel if you want to cook from it.";

export const COMMANDS = [
  { name: "/browse", desc: "Send an agent to a website" },
  { name: "/summarize", desc: "Digest the chat so far" },
  { name: "/draft", desc: "Write an email or message" },
  { name: "/remind", desc: "Add it to my calendar" },
  { name: "/screen", desc: "Look at what’s on my screen" },
];

export const APPROVAL = {
  question: "Turn these into an email?",
  options: ["Yes, draft it", "Add them to Notes instead", "Not now"],
  follows: [
    "Drafting it now. You’ll see the email on the right.",
    "Done. The three questions are in your Notes.",
    "Okay, I’ll leave it here in case you change your mind.",
  ],
};

export const planList = (title: string): ListSpec => ({
  title,
  style: "num",
  progressive: true,
  rows: [
    "Keep the same routine every day",
    "Water and daylight before your phone",
    "Give your top task the first focused block",
    "Email and messages after that block",
  ],
});

const MORNING_SHORT = "Short version: water and daylight first, then one focused block before email.";

export const GREETING =
  "Hi, I’m your GNSIS. Press the voice button to talk with me live, or type below. When a job needs its own helper, I’ll start an agent for it.";

export function homeConv(publicId: string, demo: boolean): Conv {
  return {
    id: "gnsis",
    title: "GNSIS",
    faceKey: publicId,
    kind: "home",
    req: "",
    ans: "",
    stream: 0,
    panel: "agents",
    doneText: "Replied to you",
    shortReply: "Short version: Roofer prep needs one answer from you, and Recipe finder is still cooking.",
    turns: demo
      ? [
          { role: "user", text: "Help me get ready for my meeting with the roofer", spoken: true },
          { role: "agent", text: "I started Roofer prep for that. It has a question for you when you’re ready.", stream: 999 },
          { role: "user", text: "Also find a cacio e pepe recipe for two" },
          { role: "agent", text: "Recipe finder is on it. I’ll let you know when it’s done.", stream: 999 },
        ]
      : [{ role: "agent", text: GREETING, stream: 999 }],
  };
}

export function focusConv(bornT?: number): Conv {
  return {
    id: "focus",
    title: "Morning coach",
    kind: "chat",
    req: QUESTION,
    spoken: true,
    ans: MORNING_ANSWER,
    stream: 0,
    turns: [],
    panel: "list",
    list: planList("Your morning plan"),
    doneText: "Your morning plan is ready",
    shortReply: MORNING_SHORT,
    bornT,
  };
}

export function recipeConv(req?: string): Conv {
  return {
    id: "recipe",
    title: "Recipe finder",
    kind: "agent",
    req: req || "Find a cacio e pepe recipe for two",
    ans: "",
    stream: 0,
    turns: [],
    agentT: 0,
    agentStream: 0,
    stepsOpen: true,
    panel: "browser",
    doneText: "Found a recipe for two",
    shortReply: "Short version: spaghetti, Pecorino and black pepper, tossed off the heat with a splash of pasta water.",
  };
}

export function spawnedConv(id: string, title: string, req: string, spoken: boolean, bornT: number): Conv {
  return {
    id,
    title,
    kind: "chat",
    req,
    spoken,
    ans: MORNING_ANSWER,
    stream: 0,
    turns: [],
    bornT,
    panel: "list",
    list: planList("Plan"),
    doneText: "Finished your request",
    shortReply: MORNING_SHORT,
  };
}

/** Demo agents so every state can be reviewed (Settings → Developer, or ?demo in a browser). */
export function demoAgents(): Record<string, Conv> {
  const watch = "I’m watching your inbox for the roofer’s reply. I’ll let you know as soon as it arrives.";
  const triage = "I sorted your inbox into three groups: needs a reply, read later, and receipts. Nothing looked urgent. The groups are on the right.";
  const gift = "A photo book made from Sunday’s pictures would be personal and easy to put together.";
  return {
    roof: {
      id: "roof", title: "Roofer prep", kind: "chat",
      req: "What should I ask a contractor before I accept a roof estimate?",
      ans: ROOF_ANSWER, stream: ROOF_ANSWER.length, turns: [], needs: true, approval: "pending",
      panel: "list",
      list: { title: "Questions for the roofer", style: "check", rows: ["What the price includes", "Proof of license and insurance", "Warranty in writing"] },
      doneText: "Your email draft is ready",
      shortReply: "Short version: ask what’s included, ask for proof of license and insurance, and get the warranty in writing.",
    },
    recipe: recipeConv(),
    watch: {
      id: "watch", title: "Inbox watch", kind: "chat", forever: true,
      req: "Tell me when the roofer replies about the estimate", spoken: true,
      ans: watch, stream: watch.length, turns: [], panel: "watch",
      doneText: "Stopped watching", shortReply: "Still watching. Nothing from the roofer yet.",
    },
    triage: {
      id: "triage", title: "Inbox triage", kind: "chat",
      req: "Sort my inbox and flag anything urgent",
      ans: triage, stream: triage.length, turns: [], unread: true, panel: "list",
      list: { title: "Your inbox, sorted", style: "chev", rows: ["Needs a reply", "Read later", "Receipts"] },
      doneText: "Your inbox is sorted", shortReply: "Short version: nothing urgent. A few emails need a reply.",
    },
    gift: {
      id: "gift", title: "Gift finder", kind: "chat",
      req: "Find a birthday gift idea for Dana",
      ans: gift, stream: gift.length, turns: [], archived: true, readAt: 0, panel: "list",
      list: { title: "Gift idea", style: "chev", rows: ["Photo book from Sunday’s pictures"] },
      doneText: "Found a gift idea", shortReply: "Short version: a photo book from Sunday’s pictures.",
    },
  };
}

export interface LiveSegment {
  who: "agent" | "user";
  a: number;
  b: number;
  cut?: number;
  text: string;
}

/**
 * A scripted live conversation, in tenths of a second, for the simulated host
 * only. The real host replaces every line of this with what the runtime says.
 */
export function liveScript(conv: Conv | null): LiveSegment[] {
  if (conv && conv.kind !== "home") {
    return [
      { who: "agent", a: 12, b: 52, text: "I’m here. Want to pick up where we left off?" },
      { who: "user", a: 58, b: 100, text: "Yes. Give me the short version." },
      { who: "agent", a: 106, b: 196, cut: 176, text: conv.shortReply + " Want more detail on any part?" },
      { who: "user", a: 170, b: 194, text: "Just the first part." },
      { who: "agent", a: 200, b: 250, text: "Sure, starting with the first part." },
    ];
  }
  return [
    { who: "agent", a: 12, b: 52, text: "Hi! I’m here. What’s on your mind?" },
    { who: "user", a: 58, b: 112, text: "I’m meeting the roofer tomorrow, and I’m a little nervous about it." },
    { who: "agent", a: 118, b: 200, cut: 176, text: "That makes sense. Want me to walk you through the three questions from Roofer prep, one at a time?" },
    { who: "user", a: 170, b: 194, text: "Yes, let’s do that." },
    { who: "agent", a: 200, b: 292, text: "First one: ask what the price includes, and what could change once the old roof is off." },
  ];
}
