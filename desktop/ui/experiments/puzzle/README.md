# Puzzle easter egg (on hold)

Parked, not wired into the app and not built or tested.

`sliding-puzzle-gallery.tsx` is the owner-supplied `SlidingPuzzleGallery` component, unchanged.

Agreed in mockup review so far:
- Typing or saying "let's play a game" opens the puzzle in the right-hand drawer (`AgentPanel`), the way the browser panel opens.
- The strip under the picture is removed: photo number, title, caption, moves, status, thumbnails, arrows and Solve.
- Gap fixed at 4. The only player control is a Difficulty slider mapped to `size` (Easy 3×3, Medium 4×4, Hard 5×5, Expert 6×6; the component clamps `size` to 3–6).

Still open: whether GNSIS replies in the chat and with what words; scrambling once and then waiting instead of autoplay solving; shipping the photos inside the app (the app blocks remote images).
