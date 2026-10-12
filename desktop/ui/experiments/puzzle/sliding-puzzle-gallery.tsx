"use client";

import * as React from "react";

export interface PuzzleItem {
  src: string;
  alt?: string;
  title?: string;
  caption?: string;
}

export interface SlidingPuzzleGalleryProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "onChange"> {
  items: PuzzleItem[];
  /** Tiles per side, 3–6. The bottom-right piece is held back to make the empty slot. */
  size?: number;
  index?: number;
  defaultIndex?: number;
  onIndexChange?: (index: number) => void;
  /** Solve, rest, shuffle and move on to the next photo by itself. */
  autoplay?: boolean;
  /** How long a solved photo stays up before the next shuffle, in milliseconds. */
  hold?: number;
  /** Milliseconds for a tile to slide one cell while solving. */
  moveDuration?: number;
  /** Random moves used to shuffle each photo. */
  shuffleMoves?: number;
  /** Let people slide tiles themselves with a click, a tap or the arrow keys. */
  playable?: boolean;
  /** Print the classic tile numbers on each piece. */
  numbers?: boolean;
  /** Gap between tiles in pixels while the puzzle is open. */
  gap?: number;
  /** React 19 passes `ref` as a regular prop. */
  ref?: React.Ref<HTMLDivElement>;
}

type Phase = "solving" | "solved" | "shuffling" | "playing";

interface Game {
  n: number;
  board: number[];
  photo: number;
  incoming: number | null;
  phase: Phase;
  moves: number;
  path: number[];
  pending: number[];
  spread: number;
  wait: number;
  shuffleStep: number;
  solveStep: number;
  round: number;
}

type Action =
  | { type: "step" }
  | { type: "shuffle"; to: number; walk: number[] }
  | { type: "slide"; targets: number[] }
  | { type: "solve" }
  | { type: "jump"; to: number }
  | { type: "timing"; shuffleStep: number; solveStep: number }
  | { type: "reset"; game: Game };

const IDLE_MS = 4000;
const START_MS = 450;
const SETTLE_MS = 380;
const STACK_BELOW_PX = 600;
const SIDE_BOARD_MAX = "min(560px, calc(100svh - 6rem))";
const STACKED_BOARD_MAX = "min(560px, calc(100svh - 15rem))";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function neighbors(pos: number, n: number) {
  const row = Math.floor(pos / n);
  const col = pos % n;
  const out: number[] = [];
  if (row > 0) out.push(pos - n);
  if (row < n - 1) out.push(pos + n);
  if (col > 0) out.push(pos - 1);
  if (col < n - 1) out.push(pos + 1);
  return out;
}

function solvedBoard(n: number) {
  return Array.from({ length: n * n }, (_, i) => i);
}

function isSolved(board: number[]) {
  return board.every((id, pos) => id === pos);
}

/** A random walk of the empty slot that never steps straight back, so every move adds real disorder. */
function shuffleWalk(from: number, previous: number | undefined, n: number, count: number, seed: number) {
  const rand = mulberry32(seed);
  const walk: number[] = [];
  let at = from;
  let last = previous;
  for (let i = 0; i < count; i++) {
    const options = neighbors(at, n).filter((p) => p !== last);
    const next = options[Math.floor(rand() * options.length)]!;
    walk.push(next);
    last = at;
    at = next;
  }
  return walk;
}

function moveBlank(game: Game, to: number, counted: boolean): Game {
  const blankId = game.n * game.n - 1;
  const from = game.board.indexOf(blankId);
  if (!neighbors(from, game.n).includes(to)) return game;
  const board = game.board.slice();
  board[from] = board[to]!;
  board[to] = blankId;
  const path =
    game.path.length >= 2 && game.path[game.path.length - 2] === to ? game.path.slice(0, -1) : [...game.path, to];
  return { ...game, board, path, moves: game.moves + (counted ? 1 : 0) };
}

function createGame(n: number, photo: number, shuffleMoves: number, moveDuration: number, scrambled: boolean): Game {
  let game: Game = {
    n,
    board: solvedBoard(n),
    photo,
    incoming: null,
    phase: "solving",
    moves: 0,
    path: [n * n - 1],
    pending: [],
    spread: 0,
    wait: START_MS,
    shuffleStep: Math.max(45, Math.round(moveDuration * 0.5)),
    solveStep: moveDuration,
    round: 0,
  };
  if (!scrambled) return { ...game, phase: "solved" };
  for (const target of shuffleWalk(n * n - 1, undefined, n, shuffleMoves, 7919 * (photo + 1) + n)) {
    game = moveBlank(game, target, false);
  }
  return isSolved(game.board) ? { ...game, phase: "solved", path: [n * n - 1] } : game;
}

function reducer(game: Game, action: Action): Game {
  switch (action.type) {
    case "timing":
      return { ...game, shuffleStep: action.shuffleStep, solveStep: action.solveStep };
    case "reset":
      return action.game;
    case "shuffle":
      return {
        ...game,
        incoming: action.to,
        phase: "shuffling",
        pending: action.walk,
        spread: action.walk.length,
        moves: 0,
        wait: 260,
        round: game.round + 1,
      };
    case "jump":
      return {
        ...game,
        board: solvedBoard(game.n),
        path: [game.n * game.n - 1],
        photo: action.to,
        incoming: null,
        phase: "solved",
        pending: [],
        moves: 0,
        round: game.round + 1,
      };
    case "solve":
      return game.phase === "playing" ? { ...game, phase: "solving", wait: 120 } : game;
    case "slide": {
      if (game.phase === "shuffling") return game;
      let next = game;
      for (const target of action.targets) next = moveBlank(next, target, true);
      if (next === game) return game;
      if (isSolved(next.board)) return { ...next, phase: "solved", path: [game.n * game.n - 1] };
      return { ...next, phase: "playing" };
    }
    case "step": {
      if (game.phase === "shuffling") {
        const [target, ...rest] = game.pending;
        let next = target === undefined ? game : moveBlank(game, target, false);
        if (rest.length > 0) return { ...next, pending: rest, wait: game.shuffleStep };
        next = { ...next, pending: [], photo: game.incoming ?? game.photo, incoming: null, moves: 0 };
        if (isSolved(next.board)) return { ...next, phase: "solved", path: [game.n * game.n - 1] };
        return { ...next, phase: "solving", wait: SETTLE_MS };
      }
      if (game.phase === "solving") {
        if (game.path.length <= 1 || isSolved(game.board)) return { ...game, phase: "solved", path: [game.n * game.n - 1] };
        const next = moveBlank(game, game.path[game.path.length - 2]!, true);
        if (next.path.length <= 1 || isSolved(next.board)) return { ...next, phase: "solved", path: [game.n * game.n - 1] };
        return { ...next, wait: game.solveStep };
      }
      return game;
    }
  }
}

function lineTargets(blank: number, pos: number, n: number) {
  const br = Math.floor(blank / n);
  const bc = blank % n;
  const pr = Math.floor(pos / n);
  const pc = pos % n;
  const targets: number[] = [];
  if (br === pr && bc !== pc) {
    const dir = pc > bc ? 1 : -1;
    for (let c = bc + dir; c !== pc + dir; c += dir) targets.push(br * n + c);
  } else if (bc === pc && br !== pr) {
    const dir = pr > br ? 1 : -1;
    for (let r = br + dir; r !== pr + dir; r += dir) targets.push(r * n + bc);
  }
  return targets;
}

function Chevron({ dir }: { dir: "left" | "right" }) {
  return (
    <svg viewBox="0 0 16 16" aria-hidden className="size-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d={dir === "left" ? "M10 3.5 5.5 8l4.5 4.5" : "M6 3.5 10.5 8 6 12.5"} />
    </svg>
  );
}

export function SlidingPuzzleGallery({
  items,
  size = 4,
  index: controlledIndex,
  defaultIndex = 0,
  onIndexChange,
  autoplay = true,
  hold = 2400,
  moveDuration = 150,
  shuffleMoves = 16,
  playable = true,
  numbers = false,
  gap = 4,
  ref,
  className,
  ...rest
}: SlidingPuzzleGalleryProps) {
  const n = Math.max(3, Math.min(6, Math.round(size)));
  const count = items.length;
  const startPhoto = Math.min(Math.max(0, controlledIndex ?? defaultIndex), Math.max(0, count - 1));
  const [game, dispatch] = React.useReducer(reducer, undefined, () =>
    createGame(n, startPhoto, shuffleMoves, moveDuration, autoplay),
  );
  const [reducedMotion, setReducedMotion] = React.useState(false);
  const [ready, setReady] = React.useState(false);
  const [stacked, setStacked] = React.useState(false);
  const rootRef = React.useRef<HTMLDivElement>(null);
  const sheenRef = React.useRef<HTMLDivElement>(null);
  const boardRef = React.useRef<HTMLDivElement>(null);
  const shuffleSeed = React.useRef(0);
  const target = game.incoming ?? game.photo;
  const item = items[game.photo];
  const targetItem = items[target];
  const gn = game.n;
  const blankId = gn * gn - 1;
  const solved = game.phase === "solved";

  React.useEffect(() => {
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onMotion = () => setReducedMotion(motion.matches);
    onMotion();
    motion.addEventListener("change", onMotion);
    return () => motion.removeEventListener("change", onMotion);
  }, []);

  React.useLayoutEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const measure = () => setStacked(root.clientWidth < STACK_BELOW_PX);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    return () => observer.disconnect();
  }, []);

  React.useEffect(() => {
    dispatch({ type: "timing", shuffleStep: Math.max(45, Math.round(moveDuration * 0.5)), solveStep: moveDuration });
  }, [moveDuration]);

  React.useEffect(() => {
    if (game.n === n) return;
    dispatch({ type: "reset", game: { ...createGame(n, game.incoming ?? game.photo, shuffleMoves, moveDuration, autoplay), round: game.round + 1 } });
    // Rebuild only when the grid size changes; the other values are read at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [n]);

  React.useEffect(() => {
    if (reducedMotion) dispatch({ type: "jump", to: game.incoming ?? game.photo });
    // Only a change of the motion preference should snap the board; the photo is read once at that moment.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reducedMotion]);

  React.useEffect(() => {
    let done = false;
    const markReady = () => {
      if (!done) setReady(true);
      done = true;
    };
    items.forEach((it, i) => {
      const image = new Image();
      image.decoding = "async";
      if (i === startPhoto) {
        image.onload = markReady;
        image.onerror = markReady;
      }
      image.src = it.src;
    });
    if (items.length === 0) markReady();
    const fallback = window.setTimeout(markReady, 3000);
    return () => {
      done = true;
      window.clearTimeout(fallback);
    };
    // The first photo decides when the opening solve may start; later photos are only warmed up.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  const goTo = React.useCallback(
    (to: number) => {
      if (count === 0) return;
      const next = ((to % count) + count) % count;
      onIndexChange?.(next);
      if (reducedMotion) {
        dispatch({ type: "jump", to: next });
        return;
      }
      const blank = game.board.indexOf(blankId);
      const previous = game.path.length >= 2 ? game.path[game.path.length - 2] : undefined;
      shuffleSeed.current += 1;
      const walk = shuffleWalk(blank, previous, gn, shuffleMoves, 104729 * shuffleSeed.current + 31 * next + gn);
      dispatch({ type: "shuffle", to: next, walk });
    },
    [count, onIndexChange, reducedMotion, game.board, game.path, blankId, gn, shuffleMoves],
  );

  React.useEffect(() => {
    if (controlledIndex === undefined || count === 0) return;
    if (controlledIndex !== target) goTo(controlledIndex);
    // Follow the controlled index only; goTo changes on every move.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [controlledIndex]);

  React.useEffect(() => {
    if (game.phase === "shuffling" || game.phase === "solving") {
      if (!ready && game.round === 0) return undefined;
      const timer = window.setTimeout(() => dispatch({ type: "step" }), game.wait);
      return () => window.clearTimeout(timer);
    }
    if (game.phase === "playing") {
      const timer = window.setTimeout(() => dispatch({ type: "solve" }), IDLE_MS);
      return () => window.clearTimeout(timer);
    }
    if (game.phase === "solved" && autoplay && count > 0) {
      const timer = window.setTimeout(() => goTo(game.photo + 1), hold);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [game, ready, autoplay, hold, count, goTo]);

  React.useEffect(() => {
    if (!solved || reducedMotion) return;
    const sheen = sheenRef.current;
    if (!sheen || typeof sheen.animate !== "function") return;
    const animation = sheen.animate(
      [
        { transform: "translateX(-120%)", opacity: 0 },
        { opacity: 1, offset: 0.2 },
        { transform: "translateX(120%)", opacity: 0 },
      ],
      { duration: 1100, delay: 260, easing: "cubic-bezier(0.4, 0, 0.2, 1)" },
    );
    return () => animation.cancel();
  }, [solved, game.round, game.photo, reducedMotion]);

  const slideFrom = (pos: number) => {
    if (!playable) return;
    const targets = lineTargets(game.board.indexOf(blankId), pos, gn);
    if (targets.length > 0) dispatch({ type: "slide", targets });
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!playable) return;
    const blank = game.board.indexOf(blankId);
    const row = Math.floor(blank / gn);
    const col = blank % gn;
    let to = -1;
    if (event.key === "ArrowUp" && row < gn - 1) to = blank + gn;
    else if (event.key === "ArrowDown" && row > 0) to = blank - gn;
    else if (event.key === "ArrowLeft" && col < gn - 1) to = blank + 1;
    else if (event.key === "ArrowRight" && col > 0) to = blank - 1;
    if (to < 0) return;
    event.preventDefault();
    dispatch({ type: "slide", targets: [to] });
  };

  const stepMs = reducedMotion ? 0 : game.phase === "shuffling" ? game.shuffleStep : game.solveStep;
  const status =
    game.phase === "solved" ? "Solved" : game.phase === "shuffling" ? "Shuffling" : game.phase === "playing" ? "Your move" : "Solving";
  const pad = (value: number) => String(value).padStart(2, "0");
  const piece = (src: string | undefined, id: number): React.CSSProperties => ({
    backgroundImage: src ? `url(${JSON.stringify(src)})` : undefined,
    backgroundSize: `${gn * 100}% ${gn * 100}%`,
    backgroundPosition: `${((id % gn) / (gn - 1)) * 100}% ${(Math.floor(id / gn) / (gn - 1)) * 100}%`,
  });
  const inset = solved ? 0 : gap / 2;

  const setRootRef = (node: HTMLDivElement | null) => {
    rootRef.current = node;
    if (typeof ref === "function") ref(node);
    else if (ref) ref.current = node;
  };

  const counter = (
    <span className="shrink-0 font-mono text-xs text-muted-foreground">
      {pad(target + 1)} / {pad(count)}
    </span>
  );
  const statusBadge = (
    <span aria-live="polite" data-status className="inline-flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
      <span className={cx("size-1.5 rounded-full", solved ? "bg-emerald-500" : "bg-amber-500 animate-pulse")} />
      {status}
    </span>
  );
  const movesCount = (
    <span data-moves className="font-mono text-base tabular-nums text-foreground">
      {game.moves}
    </span>
  );
  const thumbs = items.map((it, i) => (
    <button
      key={`${it.src}-${i}`}
      type="button"
      data-thumb={i}
      aria-label={`Show ${it.title ?? `photo ${i + 1}`}`}
      aria-current={i === target || undefined}
      onClick={() => i !== target && goTo(i)}
      className={cx(
        "aspect-square rounded-md bg-muted bg-cover bg-center ring-offset-2 ring-offset-background transition-[opacity,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        i === target ? "ring-2 ring-foreground" : "opacity-60 hover:opacity-100",
        stacked && "size-9 shrink-0",
      )}
      style={{ backgroundImage: `url(${JSON.stringify(it.src)})` }}
    />
  ));
  const buttonClass =
    "inline-flex h-8 items-center justify-center rounded-md border border-border bg-background text-xs font-medium transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  const prevButton = (
    <button type="button" data-prev aria-label="Previous photo" onClick={() => goTo(target - 1)} className={cx(buttonClass, "shrink-0 px-2.5")}>
      <Chevron dir="left" />
    </button>
  );
  const nextButton = (
    <button type="button" data-next aria-label="Next photo" onClick={() => goTo(target + 1)} className={cx(buttonClass, "shrink-0 px-2.5")}>
      <Chevron dir="right" />
    </button>
  );
  const actionButton = (
    <button
      type="button"
      data-action={solved ? "shuffle" : "solve"}
      onClick={() => (solved ? goTo(game.photo) : game.phase === "playing" ? dispatch({ type: "solve" }) : undefined)}
      disabled={game.phase === "shuffling" || game.phase === "solving"}
      className={cx(buttonClass, "px-3 disabled:opacity-50", stacked ? "shrink-0" : "flex-1")}
    >
      {solved ? "Shuffle" : "Solve"}
    </button>
  );

  return (
    <div
      ref={setRootRef}
      data-slot="sliding-puzzle-gallery"
      data-phase={game.phase}
      data-photo={game.photo}
      data-target={target}
      data-layout={stacked ? "stacked" : "side"}
      className={cx("w-full text-foreground", className)}
      {...rest}
    >
      <div className={cx("flex items-center", stacked ? "flex-col gap-3" : "flex-wrap gap-6")}>
        <div className={cx("min-w-0", stacked ? "w-full" : "flex-[4_1_360px]")} style={{ maxWidth: stacked ? STACKED_BOARD_MAX : SIDE_BOARD_MAX }}>
          <div className="rounded-[22px] bg-muted p-2.5 shadow-[inset_0_2px_8px_rgba(0,0,0,0.14)] ring-1 ring-border">
            <div
              ref={boardRef}
              data-board
              role="group"
              tabIndex={playable ? 0 : undefined}
              aria-label={`Sliding puzzle of ${item?.title ?? item?.alt ?? "a photo"}. Use the arrow keys to slide a tile into the gap.`}
              onKeyDown={onKeyDown}
              className="relative aspect-square w-full overflow-hidden rounded-[14px] outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-muted"
            >
              {game.board.map((id, pos) => {
                const isBlank = id === blankId;
                const style: React.CSSProperties = {
                  width: `${100 / gn}%`,
                  height: `${100 / gn}%`,
                  transform: `translate(${(pos % gn) * 100}%, ${Math.floor(pos / gn) * 100}%)`,
                  transitionProperty: "transform",
                  transitionDuration: `${stepMs}ms`,
                  transitionTimingFunction: game.phase === "shuffling" ? "linear" : "cubic-bezier(0.3, 0.7, 0.2, 1)",
                };
                const delay = game.incoming === null ? 0 : ((id * 7919) % 97) / 97;
                const layers = (
                  <>
                    <span
                      aria-hidden
                      className="absolute rounded-[7px] transition-opacity duration-300"
                      style={{ inset: gap / 2, boxShadow: "0 1px 2px rgba(0,0,0,0.3), 0 4px 10px -3px rgba(0,0,0,0.35)", opacity: solved ? 0 : 1 }}
                    />
                    <span
                      aria-hidden
                      className="absolute inset-0 bg-muted transition-[clip-path] duration-300"
                      style={{ ...piece(item?.src, id), clipPath: `inset(${inset}px round ${solved ? 0 : 7}px)` }}
                    />
                    <span
                      aria-hidden
                      data-incoming={game.incoming === null ? undefined : true}
                      className="absolute inset-0 transition-opacity"
                      style={{
                        ...piece(targetItem?.src, id),
                        clipPath: `inset(${inset}px round 7px)`,
                        opacity: game.incoming === null ? 0 : 1,
                        transitionDuration: game.incoming === null ? "0ms" : "420ms",
                        transitionDelay: `${Math.round(delay * game.spread * game.shuffleStep * 0.8)}ms`,
                      }}
                    />
                    <span
                      aria-hidden
                      className="pointer-events-none absolute rounded-[7px] transition-opacity duration-300"
                      style={{
                        inset: gap / 2,
                        boxShadow: "inset 0 1px 0 rgba(255,255,255,0.4), inset 0 -2px 0 rgba(0,0,0,0.22), inset 0 0 0 1px rgba(0,0,0,0.12)",
                        opacity: solved ? 0 : 1,
                      }}
                    />
                    {numbers && (
                      <span
                        aria-hidden
                        className="absolute left-[10%] top-[10%] rounded-full bg-black/55 px-1.5 font-mono text-[10px] leading-4 text-white backdrop-blur-sm transition-opacity duration-300"
                        style={{ opacity: solved ? 0 : 1 }}
                      >
                        {id + 1}
                      </span>
                    )}
                  </>
                );
                if (isBlank) {
                  return (
                    <div key={id} data-blank data-pos={pos} className="absolute left-0 top-0 z-[1]" style={{ ...style, transitionDuration: "0ms" }}>
                      <span
                        aria-hidden
                        className="absolute rounded-[7px] bg-foreground/[0.07] shadow-[inset_0_2px_6px_rgba(0,0,0,0.28)] transition-opacity duration-200"
                        style={{ inset: gap / 2, opacity: solved ? 0 : 1 }}
                      />
                      <span
                        aria-hidden
                        className="absolute inset-0 transition-[opacity,transform] duration-300 ease-out"
                        style={{ opacity: solved ? 1 : 0, transform: solved ? "scale(1)" : "scale(1.18)" }}
                      >
                        {layers}
                      </span>
                    </div>
                  );
                }
                return (
                  <button
                    key={id}
                    type="button"
                    tabIndex={-1}
                    data-tile={id}
                    data-pos={pos}
                    aria-label={`Tile ${id + 1}`}
                    onClick={() => slideFrom(game.board.indexOf(id))}
                    className={cx("absolute left-0 top-0 z-[2] outline-none", playable ? "cursor-pointer" : "cursor-default")}
                    style={style}
                  >
                    {layers}
                  </button>
                );
              })}
              {item && (
                <img
                  src={item.src}
                  alt={item.alt ?? item.title ?? ""}
                  data-photo-full
                  draggable={false}
                  className="pointer-events-none absolute inset-0 z-[3] size-full object-cover transition-opacity duration-300"
                  style={{ opacity: solved ? 1 : 0, transitionDelay: solved ? "220ms" : "0ms" }}
                />
              )}
              <div
                ref={sheenRef}
                aria-hidden
                className="pointer-events-none absolute inset-0 z-[4] opacity-0"
                style={{ background: "linear-gradient(105deg, transparent 38%, rgba(255,255,255,0.5) 50%, transparent 62%)" }}
              />
            </div>
          </div>
        </div>

        {stacked ? (
          <div data-panel className="flex w-full flex-col gap-2" style={{ maxWidth: STACKED_BOARD_MAX }}>
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-baseline gap-2">
                {counter}
                <h3 className="truncate text-base font-semibold tracking-tight">{targetItem?.title ?? ""}</h3>
              </div>
              <div className="flex shrink-0 items-baseline gap-3">
                <span className="inline-flex items-baseline gap-1 text-xs text-muted-foreground">
                  {movesCount}
                  <span>moves</span>
                </span>
                {statusBadge}
              </div>
            </div>
            {targetItem?.caption && <p className="-mt-1 truncate text-sm text-muted-foreground">{targetItem.caption}</p>}
            <div className="flex items-center gap-2">
              {prevButton}
              {count > 1 && <div className="flex min-w-0 flex-1 gap-1.5 overflow-x-auto p-1 [scrollbar-width:none]">{thumbs}</div>}
              {nextButton}
              {actionButton}
            </div>
          </div>
        ) : (
          <div data-panel className="flex min-w-[200px] flex-[1_1_200px] flex-col gap-5">
            <div className="flex items-center justify-between">
              {counter}
              {statusBadge}
            </div>
            <div className="min-h-[4.5rem]">
              <h3 className="text-xl font-semibold tracking-tight">{targetItem?.title ?? ""}</h3>
              {targetItem?.caption && <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{targetItem.caption}</p>}
            </div>
            <div className="flex items-baseline gap-2 text-xs text-muted-foreground">
              <span>Moves</span>
              {movesCount}
              {playable && !solved && game.phase !== "shuffling" && <span className="ml-auto">Click a tile next to the gap</span>}
            </div>
            {count > 1 && <div className="grid grid-cols-[repeat(auto-fill,minmax(48px,1fr))] gap-1.5">{thumbs}</div>}
            <div className="flex gap-2">
              {prevButton}
              {actionButton}
              {nextButton}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default SlidingPuzzleGallery;
